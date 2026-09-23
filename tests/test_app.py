"""Fast local tests; no model download, external API, or real meetings."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["PROTOCOL_DATA_DIR"] = tempfile.mkdtemp(prefix="protocol-test-")

from fastapi.testclient import TestClient  # noqa: E402
from app import inference, service, store  # noqa: E402
from app.main import app  # noqa: E402


class ProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    def test_date_normalization(self) -> None:
        self.assertEqual(inference._normalize_due("к 15 октября", "2026-09-23"), "2026-10-15")
        self.assertEqual(inference._normalize_due("ертең", "2026-09-23"), "2026-09-24")
        self.assertIsNone(inference._normalize_due("когда-нибудь", "2026-09-23"))

    def test_speaker_alignment(self) -> None:
        words = [{"start": 0, "end": .4, "text": "Гульмира", "confidence": .9},
                 {"start": .45, "end": 1, "text": "подготовьте", "confidence": .8},
                 {"start": 2, "end": 2.4, "text": "Хорошо", "confidence": .9}]
        spans = [{"start": 0, "end": 1.1, "speaker_id": "S1"},
                 {"start": 1.9, "end": 2.5, "speaker_id": "S2"}]
        turns = inference.align_words(words, spans)
        self.assertEqual([x["speaker_id"] for x in turns], ["S1", "S2"])
        self.assertEqual(turns[0]["text"], "Гульмира подготовьте")

    def test_url_validation_and_recording_lifecycle(self) -> None:
        base = {"title": "Тестовое совещание", "meeting_date": "2026-09-23", "participants": "А, Б",
                "source": "platform", "provider": "meet", "consent_confirmed": True}
        rejected = self.client.post("/api/meetings/live", json={**base, "meeting_url": "https://evil.example/a"})
        self.assertEqual(rejected.status_code, 422)
        created = self.client.post("/api/meetings/live", json={**base, "meeting_url": "https://meet.google.com/abc-defg-hij"})
        self.assertEqual(created.status_code, 201)
        meeting_id = created.json()["id"]
        chunk = self.client.post(f"/api/meetings/{meeting_id}/chunks", content=b"0" * 2000)
        self.assertEqual(chunk.status_code, 200)
        with patch("app.main.service.enqueue", side_effect=lambda mid: store.update_meeting(mid, state="queued")):
            finished = self.client.post(f"/api/meetings/{meeting_id}/finish", json={"gaps": [{"start": 1, "end": 2, "reason": "Тишина"}]})
        self.assertEqual(finished.status_code, 200)
        self.assertEqual(finished.json()["gaps"][0]["reason"], "Тишина")
        self.assertEqual(self.client.post(f"/api/meetings/{meeting_id}/chunks", content=b"x").status_code, 409)

    def test_review_export_and_audit(self) -> None:
        m = store.create_meeting(title="Проверка", meeting_date="2026-09-23", source="upload",
                                 provider=None, participants=["Председатель"], suffix=".wav")
        Path(m["media_path"]).write_bytes(b"0" * 2000)
        store.update_meeting(m["id"], state="review", segments=[{"id": "1", "start": 0.0,
            "end": 2.0, "speaker_id": "S1", "text": "Подготовьте план к 15 октября"}], speakers={"S1": "Председатель"},
            summary="Обсуждена подготовка плана работы. Председатель поручил подготовить план к 15 октября. "
                    "Секретарь сверил формулировку поручения и срок с исходной записью; документ готов к утверждению.")
        item = self.client.post(f"/api/meetings/{m['id']}/items", json={"actor": "Секретарь",
            "kind": "action", "title": "Подготовить план", "owner": "Гульмира",
            "due_text": "15 октября", "due_date": "2026-10-15", "source_segment_ids": ["1"]})
        self.assertEqual(item.status_code, 201)
        draft = self.client.get(f"/api/meetings/{m['id']}/export?format=pdf")
        self.assertEqual(draft.status_code, 200)
        self.assertTrue(draft.content.startswith(b"%PDF"))
        self.assertIn('attachment; filename="draft-protocol-', draft.headers["content-disposition"])
        approved = self.client.post(f"/api/meetings/{m['id']}/approve", json={"actor": "Секретарь", "confirmed": True})
        self.assertEqual(approved.status_code, 200)
        docx = self.client.get(f"/api/meetings/{m['id']}/export?format=docx")
        self.assertEqual(docx.status_code, 200)
        self.assertTrue(docx.content.startswith(b"PK"))
        self.assertIn('attachment; filename="protocol-approved-', docx.headers["content-disposition"])
        pdf = self.client.get(f"/api/meetings/{m['id']}/export?format=pdf")
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(pdf.content.startswith(b"%PDF"))
        self.assertEqual(self.client.patch(f"/api/meetings/{m['id']}/items/{item.json()['id']}",
            json={"actor": "Секретарь", "changes": {"title": "Другая суть"}}).status_code, 409)

    def test_summary_edit_clears_stale_topics_and_export_requires_content(self) -> None:
        meeting = store.create_meeting(title="Тематическое содержание", meeting_date="2026-09-23", source="upload",
                                       provider=None, participants=[], suffix=".wav")
        self.assertEqual(meeting["summary_topics"], [])
        store.update_meeting(meeting["id"], state="review", summary="Исходное содержание",
                             summary_topics=[{"title": "Прежняя тема"}])
        self.assertEqual(self.client.get(f"/api/meetings/{meeting['id']}/export?format=docx").status_code, 409)
        edited = self.client.patch(f"/api/meetings/{meeting['id']}/summary",
                                  json={"actor": "Секретарь", "summary": "Исправленное содержание совещания"})
        self.assertEqual(edited.status_code, 200)
        self.assertEqual(edited.json()["summary_topics"], [])
        self.assertEqual(store.get_meeting(meeting["id"])["summary_topics"], [])
        store.update_meeting(meeting["id"], state="error", segments=[{"id": "1", "start": 0.0, "end": 1.0,
                             "speaker_id": "S1", "text": "Обсудили план"}])
        with patch("app.main.exports.docx", return_value=b"PK-export") as export_docx:
            response = self.client.get(f"/api/meetings/{meeting['id']}/export?format=docx&include_transcript=true")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(export_docx.call_args.kwargs, {"include_transcript": True})
        self.assertIn("draft-protocol-", response.headers["content-disposition"])

    def test_retry_resumes_existing_transcript_and_preserves_names(self) -> None:
        meeting = store.create_meeting(title="Повтор анализа", meeting_date="2026-09-23", source="upload",
                                       provider=None, participants=["Гульмира"], suffix=".wav")
        segments = [{"id": "1", "start": 0.0, "end": 2.0, "speaker_id": "S1", "text": "План готов"}]
        store.update_meeting(meeting["id"], state="error", error="Модель анализа недоступна",
                             segments=segments, speakers={"S1": "Гульмира"})
        before = len(store.list_meetings())
        with patch("app.service._WORKER.submit") as submit:
            response = self.client.post(f"/api/meetings/{meeting['id']}/retry")
        self.assertEqual(response.status_code, 200)
        submit.assert_called_once_with(service._analyze_revision, meeting["id"])
        self.assertEqual(response.json()["speakers"], {"S1": "Гульмира"})
        self.assertEqual(response.json()["segments"], segments)
        self.assertIsNone(response.json()["error"])
        self.assertEqual(len(store.list_meetings()), before)


if __name__ == "__main__":
    unittest.main()
