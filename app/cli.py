"""Process a recording without opening the web interface."""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import date
from pathlib import Path

from . import service, store
from .main import SUFFIXES


def main() -> None:
    parser = argparse.ArgumentParser(description="Локальная обработка записи совещания")
    parser.add_argument("file", type=Path)
    parser.add_argument("--title", required=True)
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--participants", default="", help="Имена через запятую")
    args = parser.parse_args()
    source = args.file.resolve()
    if not source.is_file() or source.suffix.lower() not in SUFFIXES:
        parser.error("Нужен существующий MP3/WAV/M4A/MP4/WebM/OGG файл")
    try:
        date.fromisoformat(args.date)
    except ValueError:
        parser.error("--date должен быть YYYY-MM-DD")
    entry = store.create_meeting(title=args.title, meeting_date=args.date, source="upload",
                                 provider=None, participants=[x.strip() for x in args.participants.split(",") if x.strip()],
                                 suffix=source.suffix.lower())
    shutil.copyfile(source, entry["media_path"])
    service.process(entry["id"])
    result = store.get_meeting(entry["id"])
    print(json.dumps({"id": result["id"], "state": result["state"], "error": result["error"],
                      "summary": result["summary"], "items": result["items"]}, ensure_ascii=False, indent=2))
    if result["state"] == "error":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
