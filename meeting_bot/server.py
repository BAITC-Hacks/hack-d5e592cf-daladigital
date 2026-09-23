from __future__ import annotations

import argparse
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .audio_store import AudioStore
from .process import process_session


class Handler(BaseHTTPRequestHandler):
    store: AudioStore
    token: str
    whisper_model: Path
    ollama_model: str
    speaker_names: Path | None
    diarization_model: Path | None

    def do_POST(self) -> None:
        if self.headers.get("Authorization") != f"Bearer {self.token}":
            self.send_error(401)
            return
        parts = urlparse(self.path).path.strip("/").split("/")
        try:
            if len(parts) == 4 and parts[0] == "sessions" and parts[2] == "audio":
                length = int(self.headers.get("Content-Length", "0"))
                if length != 640:
                    raise ValueError("Expected one 20 ms PCM frame")
                self.store.append_frame(parts[1], parts[3], int(self.headers["X-Timestamp"]), self.rfile.read(length))
                self.send_response(204)
            elif len(parts) == 3 and parts[0] == "sessions" and parts[2] == "finish":
                self.store.finalize(parts[1])
                output = process_session(self.store.root, parts[1], self.whisper_model,
                                         self.ollama_model, self.speaker_names, self.diarization_model)
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                body = str(output).encode()
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            else:
                self.send_error(404)
                return
            self.end_headers()
        except (ValueError, KeyError, FileNotFoundError, RuntimeError) as exc:
            self.send_error(400, str(exc))


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Teams audio receiver")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--whisper-model", type=Path, required=True)
    parser.add_argument("--ollama-model", required=True)
    parser.add_argument("--speaker-names", type=Path)
    parser.add_argument("--diarization-model", type=Path)
    args = parser.parse_args()
    token = os.environ.get("MEETING_BOT_TOKEN")
    if not token:
        parser.error("Set MEETING_BOT_TOKEN")
    Handler.store = AudioStore(args.data)
    Handler.token = token
    Handler.whisper_model = args.whisper_model
    Handler.ollama_model = args.ollama_model
    Handler.speaker_names = args.speaker_names
    Handler.diarization_model = args.diarization_model
    with ThreadingHTTPServer((args.host, args.port), Handler) as server:
        print(f"Listening on {args.host}:{args.port}; data in {args.data}")
        server.serve_forever()


if __name__ == "__main__":
    main()
