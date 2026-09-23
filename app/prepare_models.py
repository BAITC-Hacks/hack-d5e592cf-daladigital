"""Explicit setup-time download; inference itself never downloads models."""

from __future__ import annotations

import argparse
import shutil
from urllib.request import urlopen

from .config import MODEL_DIR
from .model_files import ASR_REPO, ASR_REVISION, SPEAKER_BYTES, SPEAKER_URL, available, verify_asr


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--asr-only", action="store_true", help="Prepare only speech recognition")
    args = parser.parse_args()
    try:
        from huggingface_hub import hf_hub_download
    except ImportError as exc:
        raise SystemExit("Install requirements-ai.txt first") from exc
    destination = MODEL_DIR / "whisper-large-v3-turbo"
    destination.mkdir(parents=True, exist_ok=True)
    print(f"Preparing local ASR model in {destination}", flush=True)
    try:
        verify_asr(destination)
        asr_ready = True
    except RuntimeError:
        asr_ready = False
    if not asr_ready:
        for filename in ("config.json", "tokenizer.json", "vocabulary.json", "preprocessor_config.json", "model.bin"):
            print(f"Downloading {filename}", flush=True)
            hf_hub_download(repo_id=ASR_REPO, filename=filename, revision=ASR_REVISION,
                            local_dir=destination, force_download=filename == "model.bin")
        verify_asr(destination)
    print("ASR checksum verified", flush=True)
    if not args.asr_only:
        speaker_model = MODEL_DIR / "wespeaker" / "model.onnx"
        speaker_model.parent.mkdir(parents=True, exist_ok=True)
        if not available(speaker_model, SPEAKER_BYTES):
            print(f"Downloading speaker model to {speaker_model}", flush=True)
            part = speaker_model.with_suffix(".part")
            with urlopen(SPEAKER_URL, timeout=120) as source, part.open("wb") as target:
                shutil.copyfileobj(source, target)
            if part.stat().st_size != SPEAKER_BYTES:
                raise RuntimeError(f"Speaker model download incomplete: {part.stat().st_size}/{SPEAKER_BYTES} bytes. Retry setup.")
            part.replace(speaker_model)
        print(f"Speaker model ready ({speaker_model.stat().st_size:,} bytes)", flush=True)
    print("ASR ready. Run `ollama pull qwen3:4b` during setup for local analysis.")


if __name__ == "__main__":
    main()
