"""Fast reminder checks using temporary SQLite databases and fixed calendar dates."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("PROTOCOL_DATA_DIR", tempfile.mkdtemp(prefix="protocol-reminder-import-"))

from app import reminders, store  # noqa: E402


TODAY = date(2026, 9, 23)


class ReminderTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="protocol-reminders-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        data_patch = patch.object(store, "DATA_DIR", root)
        db_patch = patch.object(store, "DB_PATH", root / "test.sqlite3")
        data_patch.start()
        db_patch.start()
        self.addCleanup(data_patch.stop)
        self.addCleanup(db_patch.stop)
        store.init_db()
        reminders.init_db()

    def create(self, *, state: str = "approved", **changes: object) -> dict:
        meeting = store.create_meeting(title="Совещание", meeting_date="2026-09-23", source="upload",
                                       provider=None, participants=[], suffix=".wav")
        item = {"id": "action-1", "kind": "action", "title": "Подготовить план", "owner": "Гульмира",
                "status": "draft", "due_date": "2026-09-26", "reminder_recipient": "manager",
                "reminder_channel": "inbox", **changes}
        return store.update_meeting(meeting["id"], state=state, items=[item])

    def change_item(self, meeting_id: str, **changes: object) -> None:
        meeting = store.get_meeting(meeting_id)
        meeting["items"][0].update(changes)
        store.update_meeting(meeting_id, items=meeting["items"])

    def test_boundary_idempotence_read_and_restart(self) -> None:
        self.create()
        self.assertEqual(reminders.run_once(date(2026, 9, 22)), 1)
        self.assertEqual(reminders.list_notifications("manager")[0]["event"], "assigned")
        self.assertEqual(reminders.run_once(TODAY), 1)
        self.assertEqual(reminders.run_once(TODAY), 0)
        reminders.init_db()  # Application restart must preserve the notification ledger.
        self.assertEqual(reminders.run_once(TODAY), 0)
        notice = reminders.list_notifications("manager")[0]
        self.assertEqual(notice["event"], "soon")
        self.assertIn("Подготовить план", notice["message"])
        self.assertTrue(reminders.mark_read("manager", notice["id"]))
        self.assertTrue(reminders.list_notifications("manager")[0]["read_at"])
        self.assertEqual(reminders.run_once(date(2026, 9, 26)), 0)
        self.assertEqual(reminders.run_once(date(2026, 9, 27)), 1)
        self.assertEqual(reminders.run_once(date(2026, 9, 28)), 0)
        self.assertEqual([n["event"] for n in reminders.list_notifications("manager")],
                         ["overdue", "soon", "assigned"])
        reminders.init_db()
        read_notice = next(n for n in reminders.list_notifications("manager") if n["id"] == notice["id"])
        self.assertTrue(read_notice["read_at"])

    def test_overdue_at_first_run_does_not_send_soon(self) -> None:
        self.create(due_date="2026-09-22")
        self.assertEqual(reminders.run_once(TODAY), 2)
        self.assertEqual([n["event"] for n in reminders.list_notifications("manager")], ["overdue", "assigned"])

    def test_unapproved_completed_invalid_or_unassigned_items_are_skipped(self) -> None:
        self.create(state="review")
        self.create(status="done")
        self.create(status="cancelled")
        self.create(kind="decision")
        self.create(due_date="2026-02-30")
        self.create(due_date="20260923")
        self.create(reminder_recipient=None)
        self.create(reminder_recipient=" ")
        self.create(reminder_channel="email")
        self.assertEqual(reminders.run_once(TODAY), 0)
        self.assertEqual(reminders.list_notifications("manager"), [])

    def test_recipient_isolation(self) -> None:
        self.create()
        reminders.run_once(TODAY)
        notice = reminders.list_notifications("manager")[0]
        self.assertEqual(reminders.list_notifications("other"), [])
        self.assertFalse(reminders.mark_read("other", notice["id"]))
        self.assertIsNone(reminders.list_notifications("manager")[0]["read_at"])

    def test_reassignment_hides_old_notice_and_notifies_new_recipient(self) -> None:
        meeting = self.create()
        reminders.run_once(TODAY)
        old_id = reminders.list_notifications("manager")[0]["id"]
        self.change_item(meeting["id"], reminder_recipient="new-manager")
        self.assertEqual(reminders.list_notifications("manager"), [])
        self.assertFalse(reminders.mark_read("manager", old_id))
        self.assertEqual(reminders.run_once(TODAY), 2)
        self.assertEqual(len(reminders.list_notifications("new-manager")), 2)
        with store._connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 4)

    def test_changed_date_hides_stale_notice_and_gets_its_own_reminder(self) -> None:
        meeting = self.create()
        reminders.run_once(TODAY)
        self.change_item(meeting["id"], due_date="2026-09-25")
        self.assertEqual(reminders.list_notifications("manager"), [])
        self.assertEqual(reminders.run_once(TODAY), 2)
        notices = reminders.list_notifications("manager")
        self.assertEqual(len(notices), 2)
        self.assertEqual(notices[0]["due_date"], "2026-09-25")

    def test_completion_cancellation_and_removal_hide_existing_notices(self) -> None:
        for status in ("done", "cancelled"):
            with self.subTest(status=status):
                meeting = self.create()
                reminders.run_once(TODAY)
                notice = reminders.list_notifications("manager")[0]
                self.change_item(meeting["id"], status=status)
                self.assertEqual(reminders.list_notifications("manager"), [])
                self.assertFalse(reminders.mark_read("manager", notice["id"]))
                self.assertEqual(reminders.run_once(date(2026, 10, 1)), 0)
        meeting = self.create()
        reminders.run_once(TODAY)
        store.update_meeting(meeting["id"], items=[])
        self.assertEqual(reminders.list_notifications("manager"), [])

    def test_undated_assignment_is_deduplicated_after_restart(self) -> None:
        self.create(due_date=None)
        self.assertEqual(reminders.run_once(TODAY), 1)
        notice = reminders.list_notifications("manager")[0]
        self.assertEqual(notice["event"], "assigned")
        self.assertIsNone(notice["due_date"])
        self.assertIn("Срок не указан", notice["message"])
        self.assertTrue(reminders.mark_read("manager", notice["id"]))
        reminders.init_db()
        self.assertEqual(reminders.run_once(date(2027, 1, 1)), 0)
        self.assertEqual(reminders.list_notifications("manager")[0]["id"], notice["id"])
        self.assertTrue(reminders.list_notifications("manager")[0]["read_at"])

    def test_assignment_after_approval_is_immediate(self) -> None:
        from app import service

        meeting = self.create(state="review", due_date=None)
        store.update_meeting(meeting["id"], summary="Проверенное саммари. " * 8,
                             segments=[{"speaker_id": "s1", "text": "Согласовано."}],
                             speakers={"s1": "Гульмира"})
        self.assertEqual(reminders.run_once(TODAY), 0)
        with patch.object(reminders, "local_today", return_value=TODAY):
            service.approve(meeting["id"], "secretary")
        self.assertEqual([n["event"] for n in reminders.list_notifications("manager")], ["assigned"])
        self.assertEqual(reminders.run_once(TODAY), 0)

    def test_late_approval_also_notifies_deadline_immediately(self) -> None:
        from app import service

        meeting = self.create(state="review", due_date="2026-09-24")
        store.update_meeting(meeting["id"], summary="Проверенное саммари. " * 8,
                             segments=[{"speaker_id": "s1", "text": "Согласовано."}],
                             speakers={"s1": "Гульмира"})
        with patch.object(reminders, "local_today", return_value=TODAY):
            service.approve(meeting["id"], "secretary")
        self.assertEqual([n["event"] for n in reminders.list_notifications("manager")], ["soon", "assigned"])

    def test_worker_recovers_when_immediate_delivery_fails_after_approval(self) -> None:
        from app import service

        meeting = self.create(state="review", due_date=None)
        store.update_meeting(meeting["id"], summary="Проверенное саммари. " * 8,
                             segments=[{"speaker_id": "s1", "text": "Согласовано."}],
                             speakers={"s1": "Гульмира"})
        with patch.object(reminders, "run_once", side_effect=RuntimeError("temporary storage error")):
            with self.assertLogs(service.LOGGER, level="ERROR"):
                approved = service.approve(meeting["id"], "secretary")
        self.assertEqual(approved["state"], "approved")
        self.assertEqual(reminders.list_notifications("manager"), [])
        self.assertEqual(reminders.run_once(TODAY), 1)
        self.assertEqual(reminders.run_once(TODAY), 0)

    def test_almaty_midnight_drives_reminders_and_dashboard(self) -> None:
        from app import service

        class FixedClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 9, 23, 19, 1, tzinfo=timezone.utc).astimezone(tz)

        self.create(due_date="2026-09-23")
        with patch.object(reminders, "datetime", FixedClock):
            self.assertEqual(reminders.local_today(), date(2026, 9, 24))
            self.assertEqual(reminders.run_once(), 2)
            self.assertEqual(service.dashboard()["counts"]["overdue"], 1)
        self.assertEqual(reminders.list_notifications("manager")[0]["event"], "overdue")

    def test_migrate_old_check_constraint_preserves_ids_reads_and_history(self) -> None:
        meeting = self.create()
        legacy_sql = reminders._TABLE_SQL.replace("'assigned', ", "")
        with store._connect() as connection:
            connection.execute("DROP TABLE notifications")
            connection.execute(legacy_sql.format(table="notifications"))
            connection.execute("""INSERT INTO notifications
                (id, meeting_id, item_id, due_date, recipient, event, created_at, read_at)
                VALUES (17, ?, 'action-1', '2026-09-26', 'manager', 'soon', ?, ?)""",
                (meeting["id"], "2026-09-23T00:00:00+00:00", "2026-09-23T01:00:00+00:00"))
            # A deleted high ID must not be reused when rebuilding the table.
            connection.execute("UPDATE sqlite_sequence SET seq = 40 WHERE name = 'notifications'")
        reminders.init_db()
        reminders.init_db()
        notice = reminders.list_notifications("manager")[0]
        self.assertEqual(notice["id"], 17)
        self.assertEqual(notice["read_at"], "2026-09-23T01:00:00+00:00")
        self.assertEqual(reminders.run_once(TODAY), 1)
        notices = reminders.list_notifications("manager")
        self.assertGreater(notices[0]["id"], 40)
        self.assertEqual(notices[0]["event"], "assigned")
        self.assertEqual(reminders.run_once(TODAY), 0)
        self.assertEqual(reminders.run_once(date(2026, 9, 27)), 1)
        with store._connect() as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_meeting_deletion_cascades_notification_rows(self) -> None:
        meeting = self.create()
        reminders.run_once(TODAY)
        with store._connect() as connection:
            connection.execute("DELETE FROM audit_events WHERE meeting_id = ?", (meeting["id"],))
            connection.execute("DELETE FROM meetings WHERE id = ?", (meeting["id"],))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 0)
        self.assertEqual(reminders.list_notifications("manager"), [])


if __name__ == "__main__":
    unittest.main()
