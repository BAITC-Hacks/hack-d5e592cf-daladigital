"""Explicit setup-time download; inference itself never downloads models."""

from __future__ import annotations

import argparse
from urllib.request import urlopen

from .inference import MODEL_DIR


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--asr-only", action="store_true", help="Only prepare Whisper; skip diarization warmup")
    args = parser.parse_args()
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise SystemExit("Install requirements-ai.txt first") from exc
    destination = MODEL_DIR / "whisper-large-v3-turbo"
    destination.mkdir(parents=True, exist_ok=True)
    print(f"Downloading local ASR model to {destination}")
    snapshot_download(repo_id="mobiuslabsgmbh/faster-whisper-large-v3-turbo", local_dir=destination)
    if not args.asr_only:
        speaker_model = MODEL_DIR / "wespeaker" / "model.onnx"
        speaker_model.parent.mkdir(parents=True, exist_ok=True)
        # Verify size: some proxies silently truncate the response.
        expected_bytes = 26_530_309
        if not speaker_model.exists() or speaker_model.stat().st_size != expected_bytes:
            from wespeakerruntime.hub import Hub
            print(f"Downloading speaker model to {speaker_model}")
            part = speaker_model.with_suffix(".part")
            with urlopen(Hub.Assets["en"], timeout=120) as source, part.open("wb") as target:
                import shutil
                shutil.copyfileobj(source, target)
            if part.stat().st_size != expected_bytes:
                raise RuntimeError(f"Speaker model download incomplete: {part.stat().st_size}/{expected_bytes} bytes")
            part.replace(speaker_model)
        print(f"Speaker model ready ({speaker_model.stat().st_size:,} bytes)")
    print("ASR ready. Run `ollama pull qwen3:4b` during setup for local analysis.")


if __name__ == "__main__":
    main()
