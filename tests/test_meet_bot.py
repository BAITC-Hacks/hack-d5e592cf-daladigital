import tempfile
import unittest
import wave
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from meeting_bot.meet_client import DENIED, JOIN, LEAVE, meeting_finished, valid_meet_url
from meeting_bot.openai_process import (CHUNK_SECONDS, draft_protocol, iter_wav_chunks,
                                       transcribe_wav, process_wav, validate_protocol)
from meeting_bot.process import analyze_locally


class MeetBotTests(unittest.TestCase):
    def test_meet_url_validation(self):
        self.assertTrue(valid_meet_url("https://meet.google.com/abc-defg-hij"))
        self.assertFalse(valid_meet_url("https://other.example/abc-defg-hij"))
        self.assertFalse(valid_meet_url("https://meet.google.com/abc-defg-hij.evil.example"))
        self.assertTrue(DENIED.search("Вам запрещено участвовать в этой видеовстрече."))

    def test_join_never_selects_companion_or_device_transfer(self):
        self.assertIsNone(JOIN.search('Присоединиться в режиме Companion'))
        self.assertIsNone(JOIN.search('Switch here'))
        self.assertIsNone(JOIN.search('Переключиться на это устройство'))
        self.assertIsNotNone(JOIN.search('Подключиться также на этом устройстве'))
        self.assertIsNotNone(JOIN.search('Присоединиться'))
        self.assertIsNotNone(LEAVE.search('Покинуть видеовстречу'))

    def test_transient_missing_controls_do_not_end_recording(self):
        page = Mock()
        page.is_closed.return_value = False
        with patch('meeting_bot.meet_client.page_text', return_value='Reconnecting'), \
                patch('meeting_bot.meet_client.visible_button', return_value=None):
            self.assertEqual(meeting_finished(page, None, 10), (False, 10))
            self.assertEqual(meeting_finished(page, 10, 25), (False, 10))
            self.assertEqual(meeting_finished(page, 10, 41), (True, 10))
        with patch('meeting_bot.meet_client.page_text', return_value='Встреча завершена'), \
                patch('meeting_bot.meet_client.visible_button', return_value=None):
            self.assertTrue(meeting_finished(page, None, 5)[0])

    def test_missing_assignee_requires_review(self):
        protocol = {'summary': 'Итоги', 'summary_kk': 'Қорытынды',
                    'decisions': [], 'decisions_kk': [], 'tasks': [
            {'action': 'Сделать отчёт', 'action_kk': 'Есепті дайындау',
             'assignee': None, 'deadline': 'завтра', 'deadline_kk': 'ертең',
             'evidence': 'Сделать отчёт завтра', 'needs_review': False}]}
        validate_protocol(protocol)
        self.assertTrue(protocol['tasks'][0]['needs_review'])

    def test_rejects_unpaired_bilingual_content(self):
        protocol = {'summary': 'Итоги', 'summary_kk': 'Қорытынды',
                    'decisions': ['Запустить пилот'], 'decisions_kk': [], 'tasks': []}
        with self.assertRaises(ValueError):
            validate_protocol(protocol)
        protocol['decisions_kk'] = ['Пилотты бастау']
        protocol['tasks'] = [{'action': 'Сдать отчёт', 'action_kk': 'Есепті тапсыру',
                              'assignee': 'Айдана', 'deadline': 'завтра',
                              'deadline_kk': None, 'evidence': 'Сдай завтра',
                              'needs_review': False}]
        with self.assertRaises(ValueError):
            validate_protocol(protocol)

    def test_silence_is_not_sent_to_openai(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / 'silent.wav'
            with wave.open(str(audio), 'wb') as wav:
                wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                wav.writeframes(b'\0\0' * 16000)
            client = Mock()
            with self.assertRaisesRegex(ValueError, 'тишину'):
                process_wav(root, 'silent', audio, 'gpt-4o-transcribe-diarize', 'gpt-4.1-mini', client=client)
            client.audio.transcriptions.create.assert_not_called()

    def test_summary_retry_reuses_transcript_and_creates_docx(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / 'speech.wav'
            with wave.open(str(audio), 'wb') as wav:
                wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                wav.writeframes(b'\xe8\x03\x18\xfc' * 8000)
            client = Mock()
            client.audio.transcriptions.create.return_value = SimpleNamespace(segments=[
                SimpleNamespace(text='Анна, отправь отчёт завтра.', speaker='A', start=0, end=1)])
            client.responses.create.side_effect = [RuntimeError('temporary outage'), SimpleNamespace(
                output_text=json.dumps({"summary": "Отчёт", "summary_kk": "Есеп",
                                        "decisions": [], "decisions_kk": [], "tasks": []},
                                       ensure_ascii=False))]
            with self.assertRaises(RuntimeError):
                process_wav(root, 'retry', audio, 'gpt-4o-transcribe-diarize', 'gpt-4.1-mini', client=client)
            self.assertTrue((root / 'retry' / 'transcript.json').exists())
            output = process_wav(root, 'retry', audio, 'gpt-4o-transcribe-diarize', 'gpt-4.1-mini', client=client)
            client.audio.transcriptions.create.assert_called_once()
            self.assertTrue(output.is_file())

    def test_wav_chunking_keeps_offsets_and_file_size(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audio = root / "meeting.wav"
            with wave.open(str(audio), "wb") as target:
                target.setnchannels(1)
                target.setsampwidth(2)
                target.setframerate(16_000)
                target.writeframes(b"\0\0" * (CHUNK_SECONDS + 1) * 16_000)
            chunks = list(iter_wav_chunks(audio, root))
            self.assertEqual(len(chunks), 2)
            self.assertEqual(chunks[1][1], CHUNK_SECONDS)
            self.assertEqual(chunks[1][2], 1)
            self.assertLess(chunks[0][3].stat().st_size, 25_000_000)

    def test_diarized_transcription_offsets(self):
        with tempfile.TemporaryDirectory() as temporary:
            audio = Path(temporary) / "meeting.wav"
            with wave.open(str(audio), "wb") as target:
                target.setnchannels(1)
                target.setsampwidth(2)
                target.setframerate(16_000)
                target.writeframes(b"\0\0" * 16_000)
            segment = SimpleNamespace(text=" Нужно отправить отчёт. ", speaker="speaker_0",
                                      start=0.2, end=0.8)
            client = SimpleNamespace(audio=SimpleNamespace(
                transcriptions=SimpleNamespace(create=Mock(return_value=SimpleNamespace(segments=[segment])))))
            rows = transcribe_wav(audio, client, "gpt-4o-transcribe-diarize")
            self.assertEqual(rows, [{"speaker_id": "part0_speaker_0", "start": 0.2,
                                     "end": 0.8, "text": "Нужно отправить отчёт."}])

    def test_protocol_uses_structured_output(self):
        protocol = {"summary": "Обсудили отчёт", "summary_kk": "Есеп талқыланды",
                    "decisions": [], "decisions_kk": [], "tasks": []}
        create = Mock(return_value=SimpleNamespace(output_text=json.dumps(protocol, ensure_ascii=False)))
        client = SimpleNamespace(responses=SimpleNamespace(create=create))
        result = draft_protocol([{"start": 0.0, "speaker_id": "mixed", "text": "Обсудили отчёт"}],
                                client, "gpt-4.1-mini")
        self.assertEqual(result, protocol)
        self.assertEqual(create.call_args.kwargs["text"]["format"]["type"], "json_schema")
        self.assertIn("summary_kk", create.call_args.kwargs["text"]["format"]["schema"]["required"])

    def test_local_analysis_requires_both_languages(self):
        protocol = {"summary": "Обсудили отчёт", "summary_kk": "Есеп талқыланды",
                    "decisions": [], "decisions_kk": [], "tasks": []}
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps({"response": json.dumps(protocol, ensure_ascii=False)},
                                                ensure_ascii=False).encode()
        with patch('meeting_bot.process.urllib.request.urlopen', return_value=response) as urlopen:
            result = analyze_locally([{"start": 0.0, "speaker_id": "mixed",
                                       "text": "Обсудили отчёт"}], {}, "local-model")
        self.assertEqual(result, protocol)
        self.assertIn('summary_kk', json.loads(urlopen.call_args.args[0].data)['prompt'])


if __name__ == "__main__":
    unittest.main()
