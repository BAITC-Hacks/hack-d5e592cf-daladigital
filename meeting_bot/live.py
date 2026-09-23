"""Optional local, chunked live transcription preview."""

from __future__ import annotations

import json
import os
import queue
import threading
import wave
from pathlib import Path

from .audio_store import SAMPLE_RATE


class LiveTranscriber:
    def __init__(self, directory: Path, model_dir: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.model_dir = model_dir
        self.blocks: queue.Queue[tuple[float, bytes] | None] = queue.Queue(maxsize=8)
        self.dropped_blocks = 0
        self.error: Exception | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def submit(self, start_seconds: float, pcm: bytes) -> None:
        try:
            self.blocks.put_nowait((start_seconds, pcm))
        except queue.Full:
            self.dropped_blocks += 1

    def close(self) -> None:
        while self.thread.is_alive():
            try:
                self.blocks.put(None, timeout=1)
                break
            except queue.Full:
                continue
        self.thread.join()
        if self.error:
            raise RuntimeError("Live transcription failed") from self.error

    def _run(self) -> None:
        try:
            from faster_whisper import WhisperModel
            model = WhisperModel(str(self.model_dir),
                                 device=os.environ.get("LOCAL_ASR_DEVICE", "cpu"),
                                 compute_type=os.environ.get("LOCAL_ASR_COMPUTE", "int8"),
                                 local_files_only=True)
            preview = self.directory / "live_transcript.jsonl"
            chunk_file = self.directory / ".live_chunk.wav"
            while block := self.blocks.get():
                start_seconds, pcm = block
                with wave.open(str(chunk_file), "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(SAMPLE_RATE)
                    wav.writeframes(pcm)
                segments, _ = model.transcribe(str(chunk_file), language=None, vad_filter=True)
                with preview.open("a", encoding="utf-8") as output:
                    for segment in segments:
                        text = segment.text.strip()
                        if not text:
                            continue
                        row = {"start": round(start_seconds + segment.start, 2),
                               "end": round(start_seconds + segment.end, 2), "text": text}
                        output.write(json.dumps(row, ensure_ascii=False) + "\n")
                        print(f'[{row["start"]:.1f}s] {text}', flush=True)
                chunk_file.unlink(missing_ok=True)
        except Exception as exc:
            self.error = exc
