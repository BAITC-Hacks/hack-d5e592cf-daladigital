import tempfile
import sys
import types
import unittest
import wave
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from meeting_bot.audio_store import AudioStore
from meeting_bot.process import write_docx
from meeting_bot.live import LiveTranscriber


class AudioStoreTest(unittest.TestCase):
    def test_preserves_speaker_timing(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = AudioStore(Path(temporary))
            frame = b"\x01\x00" * 320
            store.append_frame("call_1", "42", 10_000_000, frame)
            store.append_frame("call_1", "42", 10_200_000, frame)
            store.append_frame("call_1", "77", 10_400_000, frame)
            tracks = store.finalize("call_1")
            self.assertEqual(len(tracks), 2)
            with wave.open(str(Path(temporary) / "call_1" / "speaker_77.wav"), "rb") as track:
                self.assertEqual(track.getframerate(), 16000)
                self.assertEqual(track.getnframes(), 960)

    def test_rejects_unsafe_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                AudioStore(Path(temporary)).append_frame("../escape", "1", 0, b"\0" * 640)

    def test_docx_contains_tasks(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "protocol.docx"
            write_docx(path, {"summary": "Обсудили запуск", "summary_kk": "Іске қосу талқыланды",
                              "decisions": [], "decisions_kk": [], "tasks": [
                {"action": "Подготовить презентацию", "action_kk": "Таныстырылымды дайындау",
                 "assignee": "Айдана", "deadline": "пятница", "deadline_kk": "жұма",
                 "evidence": "Айдана, подготовь презентацию", "needs_review": False}
            ]}, [], {})
            with ZipFile(path) as docx:
                text = docx.read("word/document.xml").decode()
            self.assertIn("Подготовить презентацию", text)
            self.assertIn("Таныстырылымды дайындау", text)
            self.assertIn("Жиналыс хаттамасы", text)
            self.assertIn("Айдана", text)

    def test_legacy_protocol_still_renders(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "protocol.docx"
            write_docx(path, {"summary": "Старый протокол", "decisions": [], "tasks": []}, [], {})
            with ZipFile(path) as docx:
                text = docx.read("word/document.xml").decode()
            self.assertIn("Старый протокол", text)
            self.assertNotIn("Жиналыс хаттамасы", text)

    def test_live_preview_stays_local_and_writes_text(self):
        class FakeModel:
            def __init__(self, *args, **kwargs):
                self.assert_local = kwargs["local_files_only"]

            def transcribe(self, *args, **kwargs):
                return [types.SimpleNamespace(start=0.0, end=0.02, text=" Привет")], None

        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(sys.modules, {"faster_whisper": types.SimpleNamespace(WhisperModel=FakeModel)}):
                preview = LiveTranscriber(Path(temporary), Path(temporary))
                preview.submit(5.0, b"\0" * 640)
                preview.close()
            text = (Path(temporary) / "live_transcript.jsonl").read_text(encoding="utf-8")
            self.assertIn("Привет", text)
            self.assertIn('"start": 5.0', text)



if __name__ == "__main__":
    unittest.main()
