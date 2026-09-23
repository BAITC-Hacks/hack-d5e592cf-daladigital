"""Durable local storage and an auditable protocol lifecycle."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DATA_DIR


DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
DB_PATH = DATA_DIR / "protocols.sqlite3"
_LOCK = threading.RLock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def init_db() -> None:
    with _connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS meetings (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                meeting_date TEXT NOT NULL,
                source TEXT NOT NULL,
                provider TEXT,
                participants_json TEXT NOT NULL,
                media_path TEXT NOT NULL,
                state TEXT NOT NULL,
                stage TEXT NOT NULL,
                error TEXT,
                summary TEXT NOT NULL DEFAULT '',
                summary_topics_json TEXT NOT NULL DEFAULT '[]',
                segments_json TEXT NOT NULL DEFAULT '[]',
                items_json TEXT NOT NULL DEFAULT '[]',
                speakers_json TEXT NOT NULL DEFAULT '{}',
                gaps_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                approved_at TEXT
            );
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id TEXT NOT NULL REFERENCES meetings(id),
                actor TEXT NOT NULL,
                event_type TEXT NOT NULL,
                details_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        # Serialize the check and ALTER across processes starting against an old archive.
        connection.execute("BEGIN IMMEDIATE")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(meetings)")}
        if "summary_topics_json" not in columns:
            connection.execute("ALTER TABLE meetings ADD COLUMN summary_topics_json TEXT NOT NULL DEFAULT '[]'")


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    for key in ("participants_json", "segments_json", "items_json", "speakers_json", "gaps_json", "summary_topics_json"):
        item[key.removesuffix("_json")] = json.loads(item.pop(key))
    return item


def audit(meeting_id: str, actor: str, event_type: str, details: dict[str, Any]) -> None:
    with _connect() as connection:
        connection.execute(
            "INSERT INTO audit_events(meeting_id,actor,event_type,details_json,created_at) VALUES(?,?,?,?,?)",
            (meeting_id, actor, event_type, json.dumps(details, ensure_ascii=False), now()),
        )


def create_meeting(
    *, title: str, meeting_date: str, source: str, provider: str | None,
    participants: list[str], suffix: str,
) -> dict[str, Any]:
    meeting_id = uuid.uuid4().hex
    meeting_dir = DATA_DIR / meeting_id
    meeting_dir.mkdir(mode=0o700)
    media_path = str(meeting_dir / f"source{suffix}")
    timestamp = now()
    with _LOCK, _connect() as connection:
        connection.execute(
            """INSERT INTO meetings
            (id,title,meeting_date,source,provider,participants_json,media_path,state,stage,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (meeting_id, title.strip(), meeting_date, source, provider,
             json.dumps(participants, ensure_ascii=False), media_path, "recording" if source != "upload" else "queued",
             "Ожидание записи" if source != "upload" else "В очереди", timestamp, timestamp),
        )
    audit(meeting_id, "system", "created", {"source": source, "provider": provider})
    return get_meeting(meeting_id)


def get_meeting(meeting_id: str) -> dict[str, Any]:
    with _connect() as connection:
        row = connection.execute("SELECT * FROM meetings WHERE id=?", (meeting_id,)).fetchone()
    if row is None:
        raise KeyError(meeting_id)
    return _decode(row)


def list_meetings() -> list[dict[str, Any]]:
    with _connect() as connection:
        rows = connection.execute("SELECT * FROM meetings ORDER BY created_at DESC").fetchall()
    return [_decode(row) for row in rows]


_JSON_FIELDS = {"participants", "segments", "items", "speakers", "gaps", "summary_topics"}
_EDITABLE_FIELDS = _JSON_FIELDS | {"state", "stage", "error", "summary", "approved_at"}


def update_meeting(meeting_id: str, **changes: Any) -> dict[str, Any]:
    if not changes or set(changes) - _EDITABLE_FIELDS:
        raise ValueError("Unsupported meeting update")
    columns: list[str] = []
    values: list[Any] = []
    for key, value in changes.items():
        column = f"{key}_json" if key in _JSON_FIELDS else key
        columns.append(f"{column}=?")
        values.append(json.dumps(value, ensure_ascii=False) if key in _JSON_FIELDS else value)
    columns.append("updated_at=?")
    values.extend((now(), meeting_id))
    with _LOCK, _connect() as connection:
        cursor = connection.execute(f"UPDATE meetings SET {', '.join(columns)} WHERE id=?", values)
        if cursor.rowcount == 0:
            raise KeyError(meeting_id)
    return get_meeting(meeting_id)


def edit_item(meeting_id: str, item_id: str, changes: dict[str, Any], actor: str) -> dict[str, Any]:
    allowed = {"title", "description", "owner", "due_text", "due_date", "status", "review_note",
               "reminder_recipient", "reminder_channel"}
    if set(changes) - allowed:
        raise ValueError("Unsupported item field")
    with _LOCK:
        meeting = get_meeting(meeting_id)
        if meeting["state"] in {"queued", "processing", "recording"}:
            raise ValueError("Дождитесь завершения обработки")
        if meeting["state"] == "approved" and set(changes) - {"status", "reminder_recipient", "reminder_channel"}:
            raise ValueError("После утверждения можно менять только статус и настройки напоминаний")
        found = next((item for item in meeting["items"] if item["id"] == item_id), None)
        if found is None:
            raise KeyError(item_id)
        before = {key: found.get(key) for key in changes}
        found.update(changes)
        update_meeting(meeting_id, items=meeting["items"])
        audit(meeting_id, actor, "item_edited", {"item_id": item_id, "before": before, "after": changes})
        return found


def add_item(meeting_id: str, item: dict[str, Any], actor: str) -> dict[str, Any]:
    with _LOCK:
        meeting = get_meeting(meeting_id)
        if meeting["state"] != "review":
            raise ValueError("Добавлять элементы можно только до утверждения")
        valid_ids = {segment["id"] for segment in meeting["segments"]}
        if not item["source_segment_ids"] or set(item["source_segment_ids"]) - valid_ids:
            raise ValueError("Укажите существующий номер исходной реплики")
        item = {"id": uuid.uuid4().hex[:10], **item}
        update_meeting(meeting_id, items=[*meeting["items"], item])
        audit(meeting_id, actor, "item_added", {"item": item})
        return item


def edit_speaker(meeting_id: str, speaker_id: str, name: str, actor: str) -> dict[str, Any]:
    with _LOCK:
        meeting = get_meeting(meeting_id)
        if meeting["state"] == "approved":
            raise ValueError("Утверждённый протокол нельзя изменить без новой версии")
        known = {segment["speaker_id"] for segment in meeting["segments"]}
        if speaker_id not in known:
            raise KeyError(speaker_id)
        before = meeting["speakers"].get(speaker_id)
        meeting["speakers"][speaker_id] = name.strip()
        update_meeting(meeting_id, speakers=meeting["speakers"])
        audit(meeting_id, actor, "speaker_named", {"speaker_id": speaker_id, "before": before, "after": name})
        return meeting["speakers"]


def delete_meeting(meeting_id: str, actor: str) -> None:
    """Remove one finished meeting and its media; never follow a stored arbitrary path."""
    if not re.fullmatch(r"[a-f0-9]{32}", meeting_id):
        raise ValueError("Некорректный идентификатор совещания")
    with _LOCK:
        meeting = get_meeting(meeting_id)
        if meeting["state"] in {"recording", "queued", "processing"}:
            raise ValueError("Нельзя удалить запись во время записи или обработки")
        directory = DATA_DIR / meeting_id
        if directory.is_symlink() or Path(meeting["media_path"]).parent.resolve() != directory.resolve():
            raise ValueError("Расположение записи требует проверки администратором")
        quarantine = DATA_DIR / (".delete-" + meeting_id + "-" + uuid.uuid4().hex)
        moved = directory.exists()
        if moved:
            directory.rename(quarantine)
        try:
            with _connect() as connection:
                connection.execute("PRAGMA secure_delete=ON")
                connection.execute("DELETE FROM audit_events WHERE meeting_id=?", (meeting_id,))
                # Reminder table has ON DELETE CASCADE. Keep a content-free deletion receipt.
                connection.execute("CREATE TABLE IF NOT EXISTS deletion_events "
                                   "(meeting_id TEXT, actor TEXT, created_at TEXT)")
                connection.execute("INSERT INTO deletion_events VALUES(?,?,?)", (meeting_id, actor, now()))
                connection.execute("DELETE FROM meetings WHERE id=?", (meeting_id,))
        except Exception:
            if moved:
                quarantine.rename(directory)
            raise
        if moved:
            shutil.rmtree(quarantine)


init_db()
