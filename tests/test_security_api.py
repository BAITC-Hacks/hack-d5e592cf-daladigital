"""Fast API authorization checks against a temporary archive, with no models."""

from __future__ import annotations

import os
import json
import tempfile
import unittest
from contextlib import ExitStack
from datetime import date
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("PROTOCOL_DATA_DIR", tempfile.mkdtemp(prefix="protocol-security-import-"))

from fastapi.testclient import TestClient  # noqa: E402
from app import auth, recording_notice, reminders, store  # noqa: E402
from app.main import COOKIE_NAME, app  # noqa: E402


PASSWORD = "strong-test-password"


class SecurityApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="protocol-security-")))
        self.stack.enter_context(patch.object(store, "DATA_DIR", self.directory))
        self.stack.enter_context(patch.object(store, "DB_PATH", self.directory / "archive.sqlite3"))
        self.stack.enter_context(patch.object(auth, "PASSWORD_ITERATIONS", 1_000))
        # The reminders unit suite tests scheduling. Keep integration tests fully
        # deterministic and invoke reminder generation explicitly where needed.
        self.stack.enter_context(patch.object(reminders.ReminderScheduler, "start"))
        store.init_db()
        self.client = self.stack.enter_context(TestClient(app))

    def setup_admin(self):
        return self.client.post("/api/auth/setup", json={"username": "admin", "display_name": "Администратор",
                                                        "password": PASSWORD, "role": "viewer"})

    def as_role(self, role: str) -> dict:
        if not auth.is_configured():
            self.assertEqual(self.setup_admin().status_code, 200)
            auth.create_user("secretary", "Секретарь", PASSWORD, "secretary")
            auth.create_user("viewer", "Наблюдатель", PASSWORD, "viewer")
        response = self.client.post("/api/auth/login", json={"username": role, "password": PASSWORD})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.client.headers["x-csrf-token"] = data["csrf"]
        return data

    def meeting(self, *, state: str = "review", recipient: str | None = None) -> dict:
        meeting = store.create_meeting(title="Закрытое совещание", meeting_date="2026-09-23", source="upload",
                                       provider=None, participants=["Секретарь"], suffix=".wav")
        Path(meeting["media_path"]).write_bytes(b"recorded audio")
        (Path(meeting["media_path"]).parent / "audio-16k.wav").write_bytes(b"normalized audio")
        return store.update_meeting(meeting["id"], state=state, summary="Проверенное содержание совещания",
                                    segments=[{"id": "1", "text": "Подготовить отчёт", "start": 0, "end": 1,
                                               "speaker_id": "S1"}],
                                    items=[{"id": "task1", "kind": "action", "title": "Подготовить отчёт",
                                            "status": "draft", "due_date": "2026-09-24",
                                            "reminder_recipient": recipient, "reminder_channel": "inbox"}])

    def delete(self, meeting: dict, title: str | None = None):
        return self.client.request("DELETE", f"/api/meetings/{meeting['id']}",
                                   json={"confirm_title": meeting["title"] if title is None else title})

    def test_private_routes_are_closed_before_and_after_setup(self) -> None:
        meeting = self.meeting()
        routes = ["/api/meetings", f"/api/meetings/{meeting['id']}",
                  f"/api/meetings/{meeting['id']}/media", f"/api/meetings/{meeting['id']}/export?format=pdf",
                  "/api/dashboard", "/api/docs", "/openapi.json", "/api/health", "/api/notifications",
                  "/api/users", "/api/auth/me", "/api/recording-notice"]
        for configured in (False, True):
            if configured:
                self.setup_admin()
                self.client.cookies.clear()
            for path in routes:
                with self.subTest(configured=configured, path=path):
                    self.assertEqual(self.client.get(path).status_code, 401)
            self.assertEqual(self.client.post("/api/meetings/live", json={}).status_code, 401)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_setup_once_cookie_and_revocable_logout(self) -> None:
        self.assertFalse(self.client.get("/api/auth/status").json()["configured"])
        response = self.setup_admin()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"]["role"], "admin")
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)
        self.assertNotIn("password", response.text)
        self.assertEqual(self.setup_admin().status_code, 409)
        old_token = self.client.cookies.get(COOKIE_NAME)
        self.client.headers["x-csrf-token"] = response.json()["csrf"]
        self.assertEqual(self.client.post("/api/auth/logout").status_code, 200)
        self.client.cookies.set(COOKIE_NAME, old_token)
        self.assertEqual(self.client.get("/api/meetings").status_code, 401)

    def test_viewer_reads_but_cannot_mutate_or_create_users(self) -> None:
        self.as_role("viewer")
        meeting = self.meeting()
        self.assertEqual(self.client.get("/api/meetings").status_code, 200)
        self.assertEqual(self.client.get("/api/dashboard").status_code, 200)
        self.assertEqual(self.client.get(f"/api/meetings/{meeting['id']}/media").content, b"recorded audio")
        with patch("app.main.exports.pdf", return_value=b"%PDF-test"):
            self.assertEqual(self.client.get(f"/api/meetings/{meeting['id']}/export?format=pdf").status_code, 200)
        self.assertEqual(self.client.patch(f"/api/meetings/{meeting['id']}/summary",
                                          json={"actor": "admin", "summary": "Изменено"}).status_code, 403)
        self.assertEqual(self.client.post("/api/meetings/live", json={}).status_code, 403)
        self.assertEqual(self.client.post("/api/users", json={}).status_code, 403)
        self.assertEqual(self.delete(meeting).status_code, 403)
        self.assertEqual(store.get_meeting(meeting["id"])["summary"], meeting["summary"])

    def test_secretary_edit_uses_authenticated_actor_and_cannot_administer(self) -> None:
        self.as_role("secretary")
        meeting = self.meeting()
        response = self.client.patch(f"/api/meetings/{meeting['id']}/summary",
                                     json={"actor": "Поддельный администратор", "summary": "Уточнённое содержание"})
        self.assertEqual(response.status_code, 200)
        with store._connect() as connection:
            row = connection.execute("SELECT actor FROM audit_events WHERE meeting_id=? AND event_type='summary_edited'",
                                     (meeting["id"],)).fetchone()
        self.assertEqual(row["actor"], "secretary")
        self.assertEqual(self.client.get("/api/users").status_code, 200)
        self.assertEqual(self.client.post("/api/users", json={}).status_code, 403)
        self.assertEqual(self.delete(meeting).status_code, 403)

    def test_mutations_require_matching_csrf_and_origin(self) -> None:
        self.as_role("secretary")
        meeting = self.meeting()
        url = f"/api/meetings/{meeting['id']}/summary"
        body = {"actor": "secretary", "summary": "Новое содержание"}
        csrf = self.client.headers.pop("x-csrf-token")
        self.assertEqual(self.client.patch(url, json=body).status_code, 403)
        self.assertEqual(self.client.patch(url, json=body, headers={"x-csrf-token": "forged"}).status_code, 403)
        self.client.headers["x-csrf-token"] = csrf
        self.assertEqual(self.client.patch(url, json=body, headers={"origin": "https://hostile.example"}).status_code, 403)
        self.assertEqual(store.get_meeting(meeting["id"])["summary"], meeting["summary"])
        self.assertEqual(self.client.patch(url, json=body, headers={"origin": "http://testserver"}).status_code, 200)
        self.assertEqual(self.client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD},
                                          headers={"origin": "https://hostile.example"}).status_code, 403)

    def test_notifications_are_recipient_only_and_viewer_can_mark_own(self) -> None:
        self.as_role("viewer")
        self.meeting(state="approved", recipient="viewer")
        self.meeting(state="approved", recipient="secretary")
        self.assertEqual(reminders.run_once(date(2026, 9, 23)), 4)
        notifications = self.client.get("/api/notifications?username=secretary").json()
        self.assertEqual(len(notifications), 2)
        self.assertEqual({notice["recipient"] for notice in notifications}, {"viewer"})
        self.assertEqual({notice["event"] for notice in notifications}, {"assigned", "soon"})
        own_id = notifications[0]["id"]
        self.assertEqual(self.client.post(f"/api/notifications/{own_id}/read").status_code, 200)
        self.assertIsNotNone(self.client.get("/api/notifications").json()[0]["read_at"])
        foreign_id = reminders.list_notifications("secretary")[0]["id"]
        self.assertEqual(self.client.post(f"/api/notifications/{foreign_id}/read").status_code, 404)
        self.assertIsNone(reminders.list_notifications("secretary")[0]["read_at"])

    def test_email_is_separate_from_login_and_visible_in_profile(self) -> None:
        self.as_role("admin")
        response = self.client.post("/api/users", json={
            "username": "d.omarov", "display_name": "Данияр Омаров", "password": PASSWORD,
            "role": "viewer", "email": "d.omarov@samruk-demo.test",
        })
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["email"], "d.omarov@samruk-demo.test")
        self.assertEqual(response.json()["username"], "d.omarov")
        login = self.client.post("/api/auth/login", json={"username": "d.omarov", "password": PASSWORD})
        self.assertEqual(login.status_code, 200)
        self.assertEqual(self.client.get("/api/auth/me").json()["user"]["email"], "d.omarov@samruk-demo.test")
        self.assertNotIn("password_hash", response.json())

    def test_undated_assignment_is_notified_only_after_approval(self) -> None:
        self.as_role("secretary")
        meeting = self.meeting()
        store.update_meeting(meeting["id"], speakers={"S1": "Председатель"},
                             summary="Секретарь проверил содержание совещания, имя участника и поручение. "
                                     "Ответственный выбран из списка сотрудников. Срок исполнения пока не определён.")
        url = f"/api/meetings/{meeting['id']}/items/task1"
        response = self.client.patch(url, json={"actor": "Секретарь", "changes": {
            "due_date": None, "reminder_recipient": "viewer"}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(reminders.run_once(date(2026, 9, 23)), 0)
        self.assertEqual(reminders.list_notifications("viewer"), [])
        approved = self.client.post(f"/api/meetings/{meeting['id']}/approve",
                                    json={"actor": "Forged actor", "confirmed": True})
        self.assertEqual(approved.status_code, 200)
        notices = reminders.list_notifications("viewer")
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["event"], "assigned")
        self.assertIsNone(notices[0]["due_date"])
        self.assertEqual(reminders.run_once(date(2026, 9, 23)), 0)
        self.assertEqual(self.client.patch(url, json={"actor": "Секретарь", "changes": {
            "status": "done"}}).status_code, 200)
        self.assertEqual(reminders.list_notifications("viewer"), [])
        with store._connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM notifications WHERE meeting_id=?",
                                                (meeting["id"],)).fetchone()[0], 1)

    def test_noncanonical_deadlines_cannot_silently_disable_delivery(self) -> None:
        self.as_role("secretary")
        meeting = self.meeting()
        url = f"/api/meetings/{meeting['id']}/items/task1"
        for value in ("", "20260923", "2026-W39-3", "2026-02-30"):
            with self.subTest(value=value):
                response = self.client.patch(url, json={"actor": "Секретарь", "changes": {
                    "due_date": value, "reminder_recipient": "viewer"}})
                self.assertEqual(response.status_code, 422)
        self.assertEqual(store.get_meeting(meeting["id"])["items"][0]["due_date"], "2026-09-24")

    def test_recording_requires_current_notice_and_audits_secretary_acknowledgement(self) -> None:
        self.as_role("secretary")
        notice = self.client.get("/api/recording-notice").json()
        self.assertEqual(notice, recording_notice.get_notice())
        body = {"title": "Проверка предупреждения", "meeting_date": "2026-09-23", "source": "room",
                "consent_confirmed": True, "notice_version": notice["version"]}
        before = len(store.list_meetings())
        for changes in ({"consent_confirmed": False}, {"notice_version": "old"}, {"notice_version": ""}):
            self.assertEqual(self.client.post("/api/meetings/live", json={**body, **changes}).status_code, 422)
        self.assertEqual(len(store.list_meetings()), before)
        response = self.client.post("/api/meetings/live", json={**body, "actor": "forged", "text": "forged"})
        self.assertEqual(response.status_code, 201)
        with store._connect() as connection:
            row = connection.execute("SELECT * FROM audit_events WHERE meeting_id=? "
                                     "AND event_type='recording_notice_acknowledged'",
                                     (response.json()["id"],)).fetchone()
        self.assertEqual(row["actor"], "secretary")
        self.assertTrue(row["created_at"])
        details = json.loads(row["details_json"])
        self.assertEqual(details["text"], notice["text"])
        self.assertEqual(details["version"], notice["version"])
        self.assertEqual(details["confirmation"], "secretary_acknowledgement")

    def test_admin_deletion_requires_title_and_removes_all_meeting_content(self) -> None:
        self.as_role("admin")
        meeting = self.meeting(state="approved", recipient="admin")
        reminders.run_once(date(2026, 9, 23))
        self.assertEqual(self.delete(meeting, title=meeting["title"] + " ").status_code, 422)
        self.assertTrue(Path(meeting["media_path"]).exists())
        self.assertEqual(self.delete(meeting).status_code, 200)
        self.assertFalse(Path(meeting["media_path"]).parent.exists())
        self.assertFalse(list(self.directory.glob(".delete-*")))
        with store._connect() as connection:
            for table in ("meetings", "audit_events", "notifications"):
                column = "id" if table == "meetings" else "meeting_id"
                self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {column}=?",
                                                    (meeting["id"],)).fetchone()[0], 0)
            receipt = connection.execute("SELECT actor FROM deletion_events WHERE meeting_id=?", (meeting["id"],)).fetchone()
        self.assertEqual(receipt["actor"], "admin")
        self.assertEqual(self.client.get(f"/api/meetings/{meeting['id']}").status_code, 404)

    def test_deletion_rejects_active_meetings(self) -> None:
        self.as_role("admin")
        for state in ("recording", "queued", "processing"):
            with self.subTest(state=state):
                meeting = self.meeting(state=state)
                self.assertEqual(self.delete(meeting).status_code, 409)
                self.assertTrue(Path(meeting["media_path"]).exists())
                self.assertEqual(store.get_meeting(meeting["id"])["state"], state)

    def test_deletion_rejects_symlink_and_arbitrary_media_parent(self) -> None:
        self.as_role("admin")
        outside = self.directory / "unrelated"
        outside.mkdir()
        precious = outside / "source.wav"
        precious.write_bytes(b"must remain")
        meeting = self.meeting()
        parent = Path(meeting["media_path"]).parent
        for child in parent.iterdir():
            child.unlink()
        parent.rmdir()
        parent.symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.delete(meeting).status_code, 409)
        self.assertEqual(precious.read_bytes(), b"must remain")
        self.assertTrue(parent.is_symlink())

        second = self.meeting()
        with store._connect() as connection:
            connection.execute("UPDATE meetings SET media_path=? WHERE id=?", (str(precious), second["id"]))
        self.assertEqual(self.delete(second).status_code, 409)
        self.assertEqual(precious.read_bytes(), b"must remain")
        self.assertTrue(Path(second["media_path"]).exists())

    def test_login_rate_limit_covers_username_rotation(self) -> None:
        self.setup_admin()
        self.client.cookies.clear()
        for index in range(auth.LOGIN_MAX_FAILURES):
            self.assertEqual(self.client.post("/api/auth/login", json={"username": f"unknown{index}",
                                                                       "password": PASSWORD}).status_code, 401)
        response = self.client.post("/api/auth/login", json={"username": "yet-another", "password": PASSWORD})
        self.assertEqual(response.status_code, 429)
        self.assertIn("retry-after", response.headers)


if __name__ == "__main__":
    unittest.main()
