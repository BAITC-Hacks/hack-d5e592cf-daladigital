"""Fast readiness checks: no downloads, inference, or writes to the archive."""
from __future__ import annotations

import argparse
import importlib.metadata
import ipaddress
import json
import os
import shutil
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from urllib.request import ProxyHandler, Request, build_opener

from .config import MODEL_DIR
from .model_files import ASR_BYTES, ASR_REVISION, SPEAKER_BYTES, available, verify_asr

PACKAGES = ('fastapi', 'uvicorn', 'python-multipart', 'pydantic', 'python-docx',
            'reportlab', 'httpx', 'faster-whisper', 'diarize', 'onnxruntime',
            'ctranslate2', 'torch', 'torchaudio', 'silero-vad', 'wespeakerruntime')


def check(full_hash: bool = False) -> dict:
    checks: list[dict] = []
    versions = {}

    def add(name: str, ready: bool, detail: str) -> None:
        checks.append({'name': name, 'ready': bool(ready), 'detail': detail})

    add('python', (3, 11) <= sys.version_info[:2] <= (3, 12), sys.version.split()[0])
    for command in ('ffmpeg', 'ffprobe'):
        add(command, bool(shutil.which(command)), 'found' if shutil.which(command) else 'missing')
    for package in PACKAGES:
        try:
            versions[package] = importlib.metadata.version(package)
            add(package, True, versions[package])
        except importlib.metadata.PackageNotFoundError:
            add(package, False, 'missing: run bash scripts/setup.sh')
    directory = MODEL_DIR / 'whisper-large-v3-turbo'
    add('whisper_weights', available(directory / 'model.bin', ASR_BYTES),
        'expected byte count and local storage checked; use --full-hash for SHA-256')
    for filename in ('config.json', 'tokenizer.json', 'preprocessor_config.json', 'vocabulary.json'):
        add('whisper_' + filename, available(directory / filename), 'local model configuration')
    if full_hash:
        try:
            verify_asr(directory)
            add('whisper_sha256', True, 'matches pinned model')
        except (OSError, RuntimeError) as exc:
            add('whisper_sha256', False, str(exc))
    add('speaker_weights', available(MODEL_DIR / 'wespeaker' / 'model.onnx', SPEAKER_BYTES),
        'expected byte count and local storage checked')
    for package, asset in (
        ('faster-whisper', 'faster_whisper/assets/silero_vad_v6.onnx'),
        ('silero-vad', 'silero_vad/data/silero_vad.jit'),
    ):
        try:
            path = Path(importlib.metadata.distribution(package).locate_file(asset))
            ok = available(path)
        except importlib.metadata.PackageNotFoundError:
            ok = False
        add(package + '_vad_weights', ok, 'bundled offline VAD asset')
    try:
        from .exports import _font_path
        _font_path()
        add('pdf_font', True, 'Unicode font available')
    except RuntimeError as exc:
        add('pdf_font', False, str(exc))
    model = os.getenv('PROTOCOL_OLLAMA_MODEL', 'qwen3:4b')
    try:
        parts = urlsplit(os.getenv('PROTOCOL_OLLAMA_URL', 'http://127.0.0.1:11434/api/chat'))
        if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password:
            raise ValueError('Ollama URL must be a local HTTP(S) endpoint without credentials')
        # Do not make a readiness request to public AI services. Numeric private addresses
        # avoid unbounded DNS work and make the two-second HTTP timeout meaningful.
        host = parts.hostname
        address = ipaddress.ip_address('127.0.0.1' if host == 'localhost' else host)
        if not (address.is_loopback or address.is_private) or address.is_unspecified:
            raise ValueError('Ollama readiness requires localhost or a private IP address')
        target = urlunsplit((parts.scheme, parts.netloc, parts.path.rsplit('/', 1)[0] + '/tags', '', ''))
        # Ignore ambient HTTP proxy variables for confidential local service checks.
        with build_opener(ProxyHandler({})).open(Request(target), timeout=2) as response:
            payload = json.loads(response.read(1024 * 1024))
        if not isinstance(payload, dict) or not isinstance(payload.get('models'), list):
            raise ValueError('Unexpected Ollama tags response')
        names = {str(value.get('name', '')) for value in payload['models'] if isinstance(value, dict)}
        found = model in names or (':' not in model and model + ':latest' in names)
        add('ollama_model', found, model if found else f'{model} is not installed; run ollama pull {model}')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        add('ollama_model', False, f'{model}: {type(exc).__name__}; start local Ollama and check its address')
    return {'ready': all(row['ready'] for row in checks), 'checks': checks,
            'versions': versions, 'asr_revision': ASR_REVISION}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--full-hash', action='store_true', help='Also read all ASR weights to verify SHA-256')
    args = parser.parse_args()
    result = check(args.full_hash)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['ready'] else 1)


if __name__ == '__main__':
    main()
