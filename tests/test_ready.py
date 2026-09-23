"""Readiness decisions without loading models or contacting a service."""
import io
import os
import unittest
from unittest.mock import Mock, patch

from app import check_ready


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        patches = (
            patch.dict(os.environ, {'PROTOCOL_OLLAMA_URL': 'http://127.0.0.1:11434/api/chat',
                                    'PROTOCOL_OLLAMA_MODEL': 'qwen3:4b'}),
            patch.object(check_ready.shutil, 'which', return_value='/mock/bin/tool'),
            patch.object(check_ready.importlib.metadata, 'version', return_value='1.0'),
            patch.object(check_ready.importlib.metadata, 'distribution'),
            patch.object(check_ready, 'available', return_value=True),
            patch('app.exports._font_path', return_value='/mock/font.ttf'),
            patch.object(check_ready, 'build_opener'),
        )
        self.mocks = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        self.mocks[3].return_value.locate_file.return_value = '/mock/model.onnx'
        self.opener = self.mocks[-1].return_value
        self.opener.open.return_value = io.BytesIO(b'{"models":[{"name":"qwen3:4b"}]}')

    def test_existing_model_and_assets_are_ready_without_inference(self):
        result = check_ready.check()
        self.assertTrue(result['ready'])
        self.assertEqual(self.opener.open.call_args.kwargs['timeout'], 2)
        self.assertEqual(self.opener.open.call_args.args[0].full_url, 'http://127.0.0.1:11434/api/tags')

    def test_installed_ollama_with_wrong_model_is_not_ready(self):
        self.opener.open.return_value = io.BytesIO(b'{"models":[{"name":"other:latest"}]}')
        self.assertFalse(check_ready.check()['ready'])

    def test_missing_speaker_weights_prevent_readiness(self):
        self.mocks[4].side_effect = lambda path, *args: path.name != 'model.onnx'
        result = check_ready.check()
        self.assertFalse(result['ready'])
        self.assertFalse(next(row for row in result['checks'] if row['name'] == 'speaker_weights')['ready'])

    def test_unexpected_service_response_is_not_ready(self):
        self.opener.open.return_value = io.BytesIO(b'[]')
        self.assertFalse(check_ready.check()['ready'])

    def test_public_endpoint_is_not_contacted(self):
        with patch.dict(os.environ, {'PROTOCOL_OLLAMA_URL': 'https://8.8.8.8/api/chat'}):
            self.assertFalse(check_ready.check()['ready'])
        self.opener.open.assert_not_called()

    def test_full_hash_failure_is_reported(self):
        with patch.object(check_ready, 'verify_asr', side_effect=RuntimeError('checksum mismatch')):
            result = check_ready.check(full_hash=True)
        self.assertFalse(result['ready'])
        self.assertFalse(next(row for row in result['checks'] if row['name'] == 'whisper_sha256')['ready'])


if __name__ == '__main__':
    unittest.main()
