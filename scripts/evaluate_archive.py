"""Read one existing meeting without changing or re-running it; publish only metrics."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('evaluate_goldens', ROOT / 'tests/evaluate_goldens.py')
evaluator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluator)


def evaluate(database: Path, meeting_id: str, reference: str) -> dict:
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute('SELECT * FROM meetings WHERE id=?', (meeting_id,)).fetchone()
        if row is None:
            raise ValueError('Meeting ID not found')
        audit = dict(connection.execute('SELECT event_type, count(*) FROM audit_events WHERE meeting_id=? GROUP BY event_type', (meeting_id,)).fetchall())
    meeting = dict(row)
    for key in list(meeting):
        if key.endswith('_json'):
            meeting[key.removesuffix('_json')] = json.loads(meeting.pop(key))
    golden = json.loads((ROOT / 'tests/goldens' / f'meeting_{reference}.json').read_text())
    result = evaluator.compare(golden, meeting)
    fields = ('expected_actions', 'detected_actions', 'matched_actions', 'action_recall',
              'action_precision_screen', 'expected_speakers', 'detected_speakers', 'transcript_turns')
    relevant = {key: meeting.get(key) for key in ('items', 'summary', 'summary_topics', 'segments', 'speakers')}
    fingerprint = hashlib.sha256(json.dumps(relevant, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {
        'schema_version': 1, 'reference': f'meeting_{reference}',
        'evaluated_at_utc': datetime.now(timezone.utc).isoformat(),
        'source_result_updated_at': meeting['updated_at'], 'source_state': meeting['state'],
        'source_result_sha256': fingerprint,
        'method': 'Existing stored model result; no audio or inference rerun. Lexical screening against organizer reference, not semantic accuracy, WER or DER.',
        'privacy': 'No meeting IDs, names, transcript, task text, paths, or audio are included.',
        'audit_event_counts': audit,
        'metrics': {key: result[key] for key in fields},
        'matching': [{'gold_id': item['gold_id'], **{key: item[key] for key in (
            'action_match_confidence', 'assignee_ok', 'deadline_ok', 'source_ids_present')}} for item in result['matched']],
        'unmatched_reference_ids': [item['id'] for item in result['missing_actions']],
        'additional_actions_for_review_count': len(result['extra_actions_for_review']),
        'missing_speaker_name_count': len(result['missing_speakers']),
        'non_speaker_incorrectly_named_count': len(result['mentioned_person_wrongly_labelled_speaker']),
        'missing_summary_fact_count': len(result['missing_summary_facts']),
        'reference_summary_fact_count': len(golden['summary_facts']),
        'summary_topic_count': len(meeting.get('summary_topics', [])),
        'requires_manual_review': True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reference', choices=('1', '2'))
    parser.add_argument('database', type=Path)
    parser.add_argument('meeting_id')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    text = json.dumps(evaluate(args.database, args.meeting_id, args.reference), ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.write_text(text)
    else:
        print(text, end='')


if __name__ == '__main__':
    main()
