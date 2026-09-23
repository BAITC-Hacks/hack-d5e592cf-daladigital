"""Copy an existing local archive into the new application data directory."""

import argparse
import shutil
import sqlite3
from pathlib import Path

from .config import DATA_DIR


def merge_missing(source_db: Path, target_db: Path) -> None:
    """Append missing meetings without replacing records edited in the new archive."""
    if not target_db.is_file():
        raise SystemExit(f"Target archive does not exist: {target_db}. Run without --merge first.")
    copied: list[Path] = []
    imported = 0
    audit_count = 0
    try:
        with sqlite3.connect(source_db.as_uri() + "?mode=ro", uri=True, timeout=30) as source, \
                sqlite3.connect(target_db, timeout=30) as target:
            source.row_factory = sqlite3.Row
            source.execute("BEGIN")
            target.execute("PRAGMA foreign_keys=ON")
            rows = source.execute("SELECT * FROM meetings ORDER BY created_at").fetchall()
            target_columns = {row[1] for row in target.execute("PRAGMA table_info(meetings)")}
            known = {row[0] for row in target.execute("SELECT id FROM meetings")}
            prepared = []
            for row in rows:
                meeting = dict(row)
                meeting_id = meeting["id"]
                if meeting_id in known:
                    continue
                if not meeting_id or Path(meeting_id).name != meeting_id or meeting_id in {".", ".."}:
                    raise ValueError(f"Invalid meeting ID: {meeting_id!r}")
                if set(meeting) - target_columns:
                    raise ValueError("Target schema is missing meeting fields; update the application first.")
                media = Path(meeting["media_path"])
                if not media.is_absolute():
                    media = source_db.parent / media
                destination = DATA_DIR / meeting_id / media.name
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                media_available = media.is_file()
                if media_available:
                    media_stat = media.stat()
                    media_available = bool(media_stat.st_size and media_stat.st_blocks and not (getattr(media_stat, "st_flags", 0) & 0x40000000))
                if media_available:
                    # Exclusive creation preserves any existing recording, including orphan files.
                    with destination.open("xb") as output:
                        copied.append(destination)
                        with media.open("rb") as input_file:
                            shutil.copyfileobj(input_file, output)
                    if destination.stat().st_size != media_stat.st_size:
                        destination.unlink()
                        copied.remove(destination)
                        meeting["state"] = "error"
                        meeting["stage"] = "Ошибка обработки"
                        meeting["error"] = "Не удалось полностью прочитать исходный файл. Загрузите запись повторно."
                else:
                    meeting["state"] = "error"
                    meeting["stage"] = "Ошибка обработки"
                    meeting["error"] = "Исходный файл отсутствует или выгружен из локального хранилища. Загрузите запись повторно."
                meeting["media_path"] = str(destination.resolve())
                events = source.execute(
                    "SELECT meeting_id,actor,event_type,details_json,created_at FROM audit_events WHERE meeting_id=? ORDER BY id",
                    (meeting_id,),
                ).fetchall()
                prepared.append((meeting, [tuple(event) for event in events]))
            # File-provider downloads can take minutes: never hold the live database
            # write lock while reading or copying audio from the old archive.
            target.execute("BEGIN IMMEDIATE")
            for meeting, events in prepared:
                if target.execute("SELECT 1 FROM meetings WHERE id=?", (meeting["id"],)).fetchone():
                    continue
                columns = ','.join('"' + key.replace('"', '""') + '"' for key in meeting)
                placeholders = ','.join('?' for _ in meeting)
                target.execute(f"INSERT INTO meetings ({columns}) VALUES ({placeholders})", tuple(meeting.values()))
                target.executemany(
                    "INSERT INTO audit_events(meeting_id,actor,event_type,details_json,created_at) VALUES(?,?,?,?,?)",
                    events,
                )
                imported += 1
                audit_count += len(events)
            target.commit()
    except BaseException:
        for path in copied:
            path.unlink(missing_ok=True)
        raise
    print(f"Merged {imported} missing meetings and {audit_count} audit events into {DATA_DIR}; existing meetings and original archive retained.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--merge", action="store_true", help="Import only missing meetings into an existing archive")
    args = parser.parse_args()
    source_db = args.source.resolve() / "protocols.sqlite3"
    target_db = DATA_DIR / "protocols.sqlite3"
    if source_db == target_db.resolve():
        raise SystemExit("Source and target archive must be different.")
    if args.merge:
        merge_missing(source_db, target_db)
        return
    if target_db.exists():
        raise SystemExit(f"Target archive already exists: {target_db}. Nothing overwritten.")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{source_db}?mode=ro", uri=True) as source, sqlite3.connect(target_db) as target:
        source.backup(target)
        rows = target.execute("SELECT id, media_path FROM meetings").fetchall()
        for meeting_id, old_path in rows:
            media = Path(old_path)
            if not media.is_absolute():
                media = source_db.parent / media
            destination = DATA_DIR / meeting_id
            destination.mkdir(mode=0o700, exist_ok=True)
            media_available = media.is_file()
            if media_available:
                media_stat = media.stat()
                media_available = bool(media_stat.st_size and media_stat.st_blocks and not (getattr(media_stat, "st_flags", 0) & 0x40000000))
            copied_media = destination / media.name
            if media_available:
                with copied_media.open("xb") as output, media.open("rb") as input_file:
                    shutil.copyfileobj(input_file, output)
                if copied_media.stat().st_size != media_stat.st_size:
                    copied_media.unlink()
                    media_available = False
            if not media_available:
                target.execute(
                    "UPDATE meetings SET state='error',stage='Ошибка обработки',error=? WHERE id=?",
                    ("Исходный файл отсутствует или выгружен из локального хранилища. Загрузите запись повторно.", meeting_id),
                )
            target.execute("UPDATE meetings SET media_path=? WHERE id=?", (str(copied_media.resolve()), meeting_id))
        target.commit()
    print(f"Copied {len(rows)} meetings to {DATA_DIR}; original archive retained.")


if __name__ == "__main__":
    main()
