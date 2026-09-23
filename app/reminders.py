"""Durable, recipient-only reminders delivered to the application's inbox.

Only approved protocols produce notifications. Each task/deadline has at most
one ``assigned``, ``soon`` and ``overdue`` notification per recipient, including
after a restart. The inbox resolves text from the current protocol and hides entries
whose task has been completed, cancelled, removed, or reassigned.
"""

from __future__ import annotations

import json
import logging
import threading
from contextlib import closing
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from . import store


LOGGER = logging.getLogger(__name__)
REMINDER_DAYS = 3
LOCAL_TIMEZONE = ZoneInfo("Asia/Almaty")


def local_today() -> date:
    """Use the organisation's calendar, independent of the server timezone."""
    return datetime.now(LOCAL_TIMEZONE).date()


_TABLE_SQL = """CREATE TABLE {table} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    item_id TEXT NOT NULL,
    due_date TEXT NOT NULL,
    recipient TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'inbox' CHECK(channel = 'inbox'),
    event TEXT NOT NULL CHECK(event IN ('assigned', 'soon', 'overdue')),
    created_at TEXT NOT NULL,
    read_at TEXT,
    UNIQUE(meeting_id, item_id, due_date, recipient, event)
)"""


def _ensure_schema(connection: Any) -> None:
    # Serialize migration with worker writes, including across server processes.
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    schema = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'notifications'"
    ).fetchone()
    if schema is None:
        connection.execute(_TABLE_SQL.format(table="notifications"))
    elif "'assigned'" not in schema["sql"]:
        # SQLite cannot alter CHECK constraints. Copy explicit IDs and read states;
        # preserve the sequence too so a deleted notification ID is never reused.
        sequence = connection.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'notifications'"
        ).fetchone()
        connection.execute(_TABLE_SQL.format(table="notifications_migrating"))
        connection.execute("""INSERT INTO notifications_migrating
            (id, meeting_id, item_id, due_date, recipient, channel, event, created_at, read_at)
            SELECT id, meeting_id, item_id, due_date, recipient, channel, event, created_at, read_at
            FROM notifications""")
        connection.execute("DROP TABLE notifications")
        connection.execute("ALTER TABLE notifications_migrating RENAME TO notifications")
        if sequence is not None:
            connection.execute(
                "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = 'notifications'",
                (sequence["seq"],),
            )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS notifications_recipient_idx "
        "ON notifications(recipient, id DESC)"
    )


def init_db() -> None:
    with store._LOCK, closing(store._connect()) as connection, connection:
        _ensure_schema(connection)


def _eligible_items(meeting: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if meeting["state"] != "approved":
        return {}
    result = {}
    for item in json.loads(meeting["items_json"]):
        if item.get("kind") != "action" or item.get("status") not in {"draft", "in_progress"}:
            continue
        recipient = item.get("reminder_recipient")
        if not isinstance(recipient, str) or not recipient.strip():
            continue
        if item.get("reminder_channel", "inbox") != "inbox":
            continue
        if item.get("due_date") is not None and _deadline(item) is None:
            continue
        if isinstance(item.get("id"), str) and item["id"]:
            result[item["id"]] = item
    return result


def _deadline(item: dict[str, Any]) -> date | None:
    value = item.get("due_date")
    try:
        parsed = date.fromisoformat(value) if isinstance(value, str) else None
        return parsed if parsed is not None and parsed.isoformat() == value else None
    except ValueError:
        return None


def _deadline_key(item: dict[str, Any]) -> str:
    # NULL values do not conflict under SQLite UNIQUE, so use a stable key for
    # undated assignments internally and return null to API clients instead.
    deadline = _deadline(item)
    return deadline.isoformat() if deadline is not None else ""


def run_once(today: date | None = None) -> int:
    """Create assignments and due reminders atomically; return the new count.

    Deadlines use the Asia/Almaty calendar date, matching the task dashboard.
    Three days before the deadline through the deadline day is ``soon``;
    subsequent days are ``overdue``. Assignment works without a deadline.
    No external messages are sent.
    """
    today = today or local_today()
    created = 0
    with store._LOCK, closing(store._connect()) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        _ensure_schema(connection)
        meetings = connection.execute(
            "SELECT id, state, items_json FROM meetings WHERE state = 'approved'"
        ).fetchall()
        for row in meetings:
            for item_id, item in _eligible_items(dict(row)).items():
                events = ["assigned"]
                deadline = _deadline(item)
                if deadline is not None:
                    days_remaining = (deadline - today).days
                    if days_remaining <= REMINDER_DAYS:
                        events.append("overdue" if days_remaining < 0 else "soon")
                for event in events:
                    cursor = connection.execute(
                        """INSERT INTO notifications
                        (meeting_id, item_id, due_date, recipient, channel, event, created_at)
                        VALUES (?, ?, ?, ?, 'inbox', ?, ?)
                        ON CONFLICT(meeting_id, item_id, due_date, recipient, event) DO NOTHING""",
                        (row["id"], item_id, _deadline_key(item), item["reminder_recipient"], event, store.now()),
                    )
                    created += cursor.rowcount
    return created


def _visible_notification(row: Any) -> dict[str, Any] | None:
    value = dict(row)
    item = _eligible_items(value).get(value["item_id"])
    if item is None or _deadline_key(item) != value["due_date"] or item["reminder_recipient"] != value["recipient"]:
        return None
    value.pop("state")
    value.pop("items_json")
    value["item_title"] = item.get("title", "Поручение")
    value["owner"] = item.get("owner")
    value["due_date"] = value["due_date"] or None
    value["title"] = {"assigned": "Новое поручение", "overdue": "Поручение просрочено",
                      "soon": "Приближается срок поручения"}[value["event"]]
    deadline_text = f"Срок: {value['due_date']}." if value["due_date"] else "Срок не указан."
    value["message"] = f"{value['item_title']}. {deadline_text}"
    return value


_INBOX_SELECT = """SELECT n.*, m.title AS meeting_title, m.state, m.items_json
    FROM notifications n JOIN meetings m ON m.id = n.meeting_id"""


def list_notifications(username: str) -> list[dict[str, Any]]:
    """Return only current notifications addressed to this authenticated user."""
    with store._LOCK, closing(store._connect()) as connection, connection:
        _ensure_schema(connection)
        rows = connection.execute(
            _INBOX_SELECT + " WHERE n.recipient = ? ORDER BY n.id DESC", (username,)
        ).fetchall()
        return [visible for row in rows if (visible := _visible_notification(row)) is not None]


def mark_read(username: str, notification_id: int) -> bool:
    """Mark one's own visible reminder as read; return False for other entries."""
    with store._LOCK, closing(store._connect()) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        _ensure_schema(connection)
        row = connection.execute(
            _INBOX_SELECT + " WHERE n.recipient = ? AND n.id = ?", (username, notification_id)
        ).fetchone()
        if row is None or _visible_notification(row) is None:
            return False
        connection.execute(
            "UPDATE notifications SET read_at = COALESCE(read_at, ?) WHERE id = ? AND recipient = ?",
            (store.now(), notification_id, username),
        )
        return True


class ReminderScheduler:
    """Small lifecycle-managed worker; SQLite also prevents multi-process duplicates."""

    def __init__(self, interval_seconds: float = 60) -> None:
        if interval_seconds <= 0:
            raise ValueError("Reminder interval must be positive")
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        init_db()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="protocol-reminders", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                run_once()
            except Exception:
                LOGGER.exception("Automatic reminder check failed; will retry on the next interval")
            self._stop.wait(self.interval_seconds)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
