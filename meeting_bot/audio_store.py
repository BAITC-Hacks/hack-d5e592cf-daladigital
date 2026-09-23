from __future__ import annotations

import json
import re
import wave
from pathlib import Path
from threading import Lock


SAMPLE_RATE = 16000
FRAME_BYTES = 640  # 20 ms, mono 16-bit PCM at 16 kHz
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def safe_id(value: str) -> str:
    if not _SAFE_ID.fullmatch(value):
        raise ValueError("ID must contain only letters, digits, _ or -")
    return value


class AudioStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def append_frame(self, session: str, speaker: str, timestamp: int, pcm: bytes) -> None:
        session, speaker = safe_id(session), safe_id(speaker)
        if len(pcm) != FRAME_BYTES:
            raise ValueError(f"Expected {FRAME_BYTES} PCM bytes per 20 ms frame")
        if timestamp < 0:
            raise ValueError("Timestamp must be nonnegative")
        with self._lock:
            directory = self.root / session
            directory.mkdir(exist_ok=True)
            origin_file = directory / "origin.txt"
            if not origin_file.exists():
                origin_file.write_text(str(timestamp), encoding="ascii")
            origin = int(origin_file.read_text(encoding="ascii"))
            offset = round((timestamp - origin) * SAMPLE_RATE * 2 / 10_000_000)
            if offset < 0:
                raise ValueError("Frame predates session origin")
            offset -= offset % 2
            path = directory / f"speaker_{speaker}.pcm"
            with path.open("r+b" if path.exists() else "w+b") as file:
                file.seek(offset)
                file.write(pcm)

    def finalize(self, session: str) -> list[Path]:
        directory = self.root / safe_id(session)
        if not directory.is_dir():
            raise FileNotFoundError(session)
        outputs: list[Path] = []
        with self._lock:
            for raw in sorted(directory.glob("speaker_*.pcm")):
                output = raw.with_suffix(".wav")
                with wave.open(str(output), "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(SAMPLE_RATE)
                    with raw.open("rb") as source:
                        while chunk := source.read(1024 * 1024):
                            wav.writeframesraw(chunk)
                outputs.append(output)
        if not outputs:
            raise ValueError("No audio received")
        (directory / "audio_manifest.json").write_text(
            json.dumps({"sample_rate": SAMPLE_RATE, "speakers": [x.stem.removeprefix("speaker_") for x in outputs]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return outputs
