"""Explicit demo setup is isolated, collision-safe, and never resets user data."""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("PROTOCOL_DATA_DIR", tempfile.mkdtemp(prefix="protocol-demo-import-"))

from app import auth, demo, reminders, store  # noqa: E402


TODAY = date(2026, 9, 23)


class DemoTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="protocol-demo-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for target, attribute, value in (
            (store, "DATA_DIR", self.root), (store, "DB_PATH", self.root / "test.sqlite3"),
            (auth, "PASSWORD_ITERATIONS", 1_000),
        ):
            patcher = patch.object(target, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(reminders, "local_today", return_value=TODAY)
        patcher.start()
        self.addCleanup(patcher.stop)
        store.init_db()
        auth.init_db()

    def admin(self) -> None:
        auth.create_initial_admin("administrator", "Администратор", "existing-admin-password")

    def run_seed_cli(self) -> str:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(demo.main(["--seed"]), 0)
        return output.getvalue()

    def test_explicit_flag_and_existing_admin_are_required(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            demo.main([])
        with self.assertRaisesRegex(ValueError, "Сначала создайте администратора"):
            demo.seed_demo()
        self.assertEqual(auth.list_users(), [])
        self.assertEqual(store.list_meetings(), [])
        # A non-admin account cannot stand in for first-run administrator setup.
        auth.create_user("reader", "Наблюдатель", "existing-reader-password", "viewer")
        with self.assertRaises(ValueError):
            demo.seed_demo()
        self.assertEqual(len(auth.list_users()), 1)

    def test_seed_profiles_review_deadlines_and_recipient_isolation(self) -> None:
        self.admin()
        output = self.run_seed_cli()
        users = {user["username"]: user for user in auth.list_users()}
        credentials = dict(line.split("\t") for line in output.splitlines() if "\t" in line)
        self.assertEqual(set(credentials), {profile["username"] for profile in demo.PROFILES})
        self.assertEqual(len(set(credentials.values())), 3)
        for profile in demo.PROFILES:
            self.assertEqual({key: users[profile["username"]][key] for key in profile}, profile)
            self.assertGreaterEqual(len(credentials[profile["username"]]), 24)
            self.assertIsNotNone(auth.authenticate(profile["username"], credentials[profile["username"]]))
        approved = store.get_meeting(demo.APPROVED_ID)
        review = store.get_meeting(demo.REVIEW_ID)
        self.assertEqual(approved["state"], "approved")
        self.assertEqual(review["state"], "review")
        self.assertEqual(len(approved["items"]), 4)
        self.assertEqual([item["due_date"] for item in approved["items"][:3]],
                         [None, "2026-09-25", "2026-09-22"])
        for meeting in (approved, review):
            self.assertEqual(meeting["source"], "demo")
            self.assertIn("ДЕМО", meeting["title"])
            self.assertIn("AI-анализ", meeting["summary"])
            self.assertEqual(Path(meeting["media_path"]).parent.parent, self.root)
            self.assertFalse(Path(meeting["media_path"]).exists())
        daniyar = reminders.list_notifications("d.omarov")
        madina = reminders.list_notifications("m.serikova")
        self.assertEqual({notice["event"] for notice in daniyar}, {"assigned", "soon", "overdue"})
        self.assertEqual({notice["item_id"] for notice in daniyar}, {"demo-new", "demo-soon", "demo-overdue"})
        self.assertEqual({notice["item_id"] for notice in madina}, {"demo-madina"})
        self.assertEqual(reminders.list_notifications("a.saparova"), [])
        self.assertTrue(all(notice["meeting_id"] == demo.APPROVED_ID for notice in daniyar + madina))
        self.assertFalse(reminders.mark_read("m.serikova", daniyar[0]["id"]))

    def test_repeat_preserves_passwords_content_and_read_state(self) -> None:
        self.admin()
        first_output = self.run_seed_cli()
        first_users = auth.list_users()
        with store._connect() as connection:
            hashes = [tuple(row) for row in connection.execute("SELECT username,password_hash FROM auth_users ORDER BY username")]
        notice = reminders.list_notifications("d.omarov")[0]
        self.assertTrue(reminders.mark_read("d.omarov", notice["id"]))
        review = store.get_meeting(demo.REVIEW_ID)
        review["items"][0]["due_date"] = "2026-12-01"
        store.update_meeting(demo.REVIEW_ID, summary="Сохранённая ручная правка", items=review["items"])
        before_meetings = store.list_meetings()
        before_notices = reminders.list_notifications("d.omarov")
        second_output = self.run_seed_cli()
        self.assertNotIn("\t", second_output)
        self.assertIn("Новых аккаунтов нет", second_output)
        for line in first_output.splitlines():
            if "\t" in line:
                self.assertNotIn(line.split("\t")[1], second_output)
        self.assertEqual(auth.list_users(), first_users)
        self.assertEqual(store.list_meetings(), before_meetings)
        self.assertEqual(reminders.list_notifications("d.omarov"), before_notices)
        with store._connect() as connection:
            self.assertEqual([tuple(row) for row in connection.execute(
                "SELECT username,password_hash FROM auth_users ORDER BY username")], hashes)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0], 2)

    def test_matching_existing_account_keeps_its_password(self) -> None:
        self.admin()
        profile = demo.PROFILES[0]
        auth.create_user(profile["username"], profile["display_name"], "my-existing-demo-password",
                         profile["role"], profile["email"])
        result = demo.seed_demo()
        self.assertEqual(len(result["new_accounts"]), 2)
        self.assertNotIn(profile["username"], {account["username"] for account in result["new_accounts"]})
        self.assertIsNotNone(auth.authenticate(profile["username"], "my-existing-demo-password"))

    def test_profile_collision_aborts_all_new_accounts_and_meetings(self) -> None:
        self.admin()
        profile = demo.PROFILES[1]
        auth.create_user(profile["username"], profile["display_name"], "real-account-password",
                         profile["role"], profile["email"])
        for key, unexpected in (("display_name", "Другой человек"), ("role", "admin"), ("email", "real@example.test")):
            with self.subTest(key=key):
                with store._connect() as connection:
                    connection.execute(f"UPDATE auth_users SET {key}=? WHERE username=?", (unexpected, profile["username"]))
                with self.assertRaisesRegex(ValueError, "уже занят"):
                    demo.seed_demo()
                self.assertEqual(len(auth.list_users()), 2)
                self.assertEqual(store.list_meetings(), [])
                self.assertIsNotNone(auth.authenticate(profile["username"], "real-account-password"))
                with store._connect() as connection:
                    connection.execute(f"UPDATE auth_users SET {key}=? WHERE username=?", (profile[key], profile["username"]))

    def test_meeting_collision_is_not_overwritten(self) -> None:
        self.admin()
        meeting = store.create_meeting(title="Реальный протокол", meeting_date=TODAY.isoformat(), source="upload",
                                       provider=None, participants=[], suffix=".wav")
        with store._connect() as connection:
            connection.execute("DELETE FROM audit_events WHERE meeting_id=?", (meeting["id"],))
            connection.execute("UPDATE meetings SET id=? WHERE id=?", (demo.APPROVED_ID, meeting["id"]))
        with self.assertRaisesRegex(ValueError, "Идентификатор"):
            demo.seed_demo()
        self.assertEqual(len(auth.list_users()), 1)
        self.assertEqual(store.get_meeting(demo.APPROVED_ID)["title"], "Реальный протокол")


if __name__ == "__main__":
    unittest.main()
