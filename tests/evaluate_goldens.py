"""Compare an application meeting JSON with the organizer's two reference protocols.

This is a screening tool. Its lexical action matching suggests correspondences;
the audio and every flagged item still need a human review.
"""

from __future__ import annotations

import argparse
import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent


def normalized(value: Any) -> str:
    text = str(value or "").casefold().replace("ё", "е")
    return re.sub(r"\s+", " ", text).strip()


def contains_name(value: Any, alias: str) -> bool:
    actual = normalized(value)
    wanted = normalized(alias)
    return bool(re.search(r"(?<!\w)" + re.escape(wanted) + r"(?!\w)", actual))


def group_coverage(value: Any, groups: list[list[str]]) -> float:
    actual = normalized(value)
    if not groups:
        return 0.0
    return sum(any(normalized(term) in actual for term in group) for group in groups) / len(groups)


def action_similarity(expected: dict[str, Any], actual: dict[str, Any]) -> float:
    action = " ".join(str(actual.get(key) or "") for key in ("title", "description"))
    coverage = group_coverage(action, expected["action_terms"])
    if coverage < 0.66:
        return 0.0
    ratio = SequenceMatcher(None, normalized(expected["action"]), normalized(action)).ratio()
    return 0.8 * coverage + 0.2 * ratio


def deadline_matches(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    deadline = expected["deadline"]
    raw = normalized(actual.get("due_text"))
    resolved = normalized(actual.get("due_date"))
    if deadline["kind"] == "unspecified":
        return not resolved and raw in {"", "не указан", "не указано", "нет срока", "требует уточнения"}
    if any(normalized(alias) in raw for alias in deadline["aliases"]):
        return True
    if deadline["kind"] == "day_month" and resolved:
        try:
            month, day = int(resolved[5:7]), int(resolved[8:10])
        except (ValueError, IndexError):
            return False
        return f"{day:02d}.{month:02d}" in deadline["aliases"]
    return False


def fact_matches(expected: dict[str, Any], summary: str) -> bool:
    content = normalized(summary)
    if "any" in expected:
        return any(normalized(term) in content for term in expected["any"])
    return all(any(normalized(term) in content for term in group) for group in expected["all"])


def speaker_names(result: dict[str, Any]) -> list[str]:
    speakers = result.get("speakers") or {}
    if isinstance(speakers, dict):
        return [str(value).removesuffix(" (проверьте)") for value in speakers.values()]
    if isinstance(speakers, list):
        return [str(value.get("name", "")) if isinstance(value, dict) else str(value) for value in speakers]
    return []


def compare(golden: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    expected = golden["assignments"]
    actual = [item for item in result.get("items", [])
              if item.get("kind") == "action" and item.get("status") != "cancelled"]
    candidates: list[tuple[float, int, int]] = []
    for expected_index, task in enumerate(expected):
        for actual_index, item in enumerate(actual):
            score = action_similarity(task, item)
            if score:
                candidates.append((score, expected_index, actual_index))
    candidates.sort(reverse=True)
    used_expected: set[int] = set()
    used_actual: set[int] = set()
    matched: list[dict[str, Any]] = []
    for score, expected_index, actual_index in candidates:
        if expected_index in used_expected or actual_index in used_actual:
            continue
        used_expected.add(expected_index)
        used_actual.add(actual_index)
        task, item = expected[expected_index], actual[actual_index]
        matched.append({
            "gold_id": task["id"],
            "expected_action": task["action"],
            "actual_action": item.get("title"),
            "action_match_confidence": round(score, 3),
            "expected_assignee": task["assignee"],
            "actual_assignee": item.get("owner"),
            "assignee_ok": any(contains_name(item.get("owner"), name) for name in task["assignee_aliases"]),
            "expected_deadline": task["deadline"]["text"],
            "actual_deadline": item.get("due_text") or item.get("due_date"),
            "deadline_ok": deadline_matches(task, item),
            "source_ids_present": bool(item.get("source_segment_ids")),
        })

    names = speaker_names(result)
    missing_speakers = [name for name in golden["speakers"]
                        if not any(contains_name(actual_name, name) for actual_name in names)]
    unexpected_non_speakers = [name for name in golden.get("mentioned_non_speakers", [])
                               if any(contains_name(actual_name, name) for actual_name in names)]
    summary = result.get("summary") or ""
    missing_facts = [fact["label"] for fact in golden["summary_facts"]
                     if not fact_matches(fact, summary)]
    transcript = result.get("segments") or []
    return {
        "meeting_id": golden["meeting_id"],
        "reference_note": golden["reference_note"],
        "expected_actions": len(expected),
        "detected_actions": len(actual),
        "matched_actions": len(matched),
        "action_recall": round(len(matched) / len(expected), 3),
        "action_precision_screen": round(len(matched) / len(actual), 3) if actual else 0.0,
        "missing_actions": [{"id": task["id"], "action": task["action"]}
                            for index, task in enumerate(expected) if index not in used_expected],
        "extra_actions_for_review": [{"title": item.get("title"), "owner": item.get("owner")}
                                     for index, item in enumerate(actual) if index not in used_actual],
        "matched": sorted(matched, key=lambda item: item["gold_id"]),
        "expected_speakers": len(golden["speakers"]),
        "detected_speakers": len(names),
        "missing_speakers": missing_speakers,
        "mentioned_person_wrongly_labelled_speaker": unexpected_non_speakers,
        "missing_summary_facts": missing_facts,
        "transcript_turns": len(transcript),
        "requires_manual_review": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Сравнить JSON протокола с эталоном организаторов")
    parser.add_argument("meeting", choices=["1", "2"], help="Номер совещания")
    parser.add_argument("result", type=Path, help="JSON из GET /api/meetings/{id}")
    args = parser.parse_args()
    golden = json.loads((ROOT / "goldens" / f"meeting_{args.meeting}.json").read_text(encoding="utf-8"))
    result = json.loads(args.result.read_text(encoding="utf-8"))
    print(json.dumps(compare(golden, result), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
