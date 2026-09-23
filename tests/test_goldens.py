"""Checks for the organizer-derived reference dataset and the comparison tool."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from evaluate_goldens import compare, deadline_matches


ROOT = Path(__file__).resolve().parent


class GoldenTests(unittest.TestCase):
    def test_reference_counts_and_unique_ids(self) -> None:
        expected_counts = {1: 10, 2: 6}
        for number, count in expected_counts.items():
            golden = json.loads((ROOT / "goldens" / f"meeting_{number}.json").read_text(encoding="utf-8"))
            self.assertEqual(len(golden["assignments"]), count)
            self.assertEqual(len({item["id"] for item in golden["assignments"]}), count)

    def test_missing_deadline_is_not_filled_in(self) -> None:
        golden = json.loads((ROOT / "goldens" / "meeting_2.json").read_text(encoding="utf-8"))
        task = golden["assignments"][-1]
        self.assertTrue(deadline_matches(task, {"due_text": None, "due_date": None}))
        self.assertFalse(deadline_matches(task, {"due_text": "завтра", "due_date": None}))

    def test_mentioned_erlan_is_not_a_speaker(self) -> None:
        golden = json.loads((ROOT / "goldens" / "meeting_2.json").read_text(encoding="utf-8"))
        actual = {"items": [], "summary": "", "segments": [], "speakers": {"S1": "Ерлан"}}
        report = compare(golden, actual)
        self.assertEqual(report["mentioned_person_wrongly_labelled_speaker"], ["Ерлан"])

    def test_expected_actions_match_the_reference_dataset(self) -> None:
        for number in (1, 2):
            golden = json.loads((ROOT / "goldens" / f"meeting_{number}.json").read_text(encoding="utf-8"))
            actual = {"items": [{"kind": "action", "status": "draft", "title": item["action"],
                                  "owner": item["assignee"], "due_text": item["deadline"]["text"],
                                  "source_segment_ids": ["1"]} for item in golden["assignments"]],
                      "summary": "", "segments": [], "speakers": {}}
            report = compare(golden, actual)
            self.assertEqual(report["matched_actions"], len(golden["assignments"]))
            self.assertTrue(all(item["assignee_ok"] and item["deadline_ok"] for item in report["matched"]))


if __name__ == "__main__":
    unittest.main()
