"""Small, isolated security checks; no ASR, network, or external services."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("PROTOCOL_DATA_DIR", tempfile.mkdtemp(prefix="protocol-auth-import-"))

from app import auth, store  # noqa: E402


class AuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="protocol-auth-")
        self.addCleanup(self.directory.cleanup)
        self.db_patch = patch.object(store, "DB_PATH", Path(self.directory.name) / "test.sqlite3")
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        # Exercise the real KDF with fewer rounds to keep this suite fast.
        self.rounds_patch = patch.object(auth, "PASSWORD_ITERATIONS", 1_000)
        self.rounds_patch.start()
        self.addCleanup(self.rounds_patch.stop)
        auth.init_db()

    def admin(self) -> dict:
        return auth.create_initial_admin("Admin", "Администратор", "a-long-test-password")

    def test_atomic_bootstrap_validation_and_public_users(self) -> None:
        self.assertFalse(auth.is_configured())
        user = self.admin()
        self.assertEqual(user["username"], "admin")
        self.assertTrue(auth.is_configured())
        self.assertNotIn("password_hash", user)
        with self.assertRaisesRegex(ValueError, "уже выполнена"):
            auth.create_initial_admin("second", "Second", "a-long-test-password")
        with self.assertRaises(ValueError):
            auth.create_user("bad user", "Name", "a-long-test-password", "viewer")
        with self.assertRaises(ValueError):
            auth.create_user("normal", "Name", "short", "viewer")
        with self.assertRaises(ValueError):
            auth.create_user("normal", "Name", "a-long-test-password", "superuser")
        self.assertEqual(auth.list_users(), [user])

    def test_passwords_are_salted_and_sessions_are_hashed_revocable(self) -> None:
        self.admin()
        auth.create_user("reader", "Читатель", "a-long-test-password", "viewer")
        with store._connect() as connection:
            hashes = [row[0] for row in connection.execute("SELECT password_hash FROM auth_users")]
        self.assertNotEqual(hashes[0], hashes[1])
        self.assertTrue(all(value.startswith("pbkdf2_sha256$") for value in hashes))
        self.assertIsNone(auth.authenticate("admin", "incorrect-password"))
        token, csrf, user = auth.authenticate(" ADMIN ", "a-long-test-password")
        self.assertEqual(user["role"], "admin")
        with store._connect() as connection:
            stored = connection.execute("SELECT token_hash FROM auth_sessions").fetchone()[0]
        self.assertNotEqual(stored, token)
        self.assertEqual(len(stored), 64)
        session = auth.get_session(token)
        self.assertEqual(session["user"], user)
        self.assertTrue(auth.validate_csrf(session, csrf))
        self.assertFalse(auth.validate_csrf(session, "Неверный токен"))
        self.assertFalse(auth.validate_csrf(session, None))
        auth.logout(token)
        self.assertIsNone(auth.get_session(token))

    def test_expiry_and_role_checks(self) -> None:
        self.admin()
        auth.create_user("secretary", "Секретарь", "a-long-test-password", "secretary")
        auth.create_user("viewer", "Наблюдатель", "a-long-test-password", "viewer")
        with patch("app.auth.time.time", return_value=1_000):
            token, _, user = auth.authenticate("viewer", "a-long-test-password")
            self.assertIsNotNone(auth.get_session(token))
        self.assertFalse(auth.has_role(user, {"admin", "secretary"}))
        self.assertTrue(auth.has_role(user, {"admin", "secretary", "viewer"}))
        self.assertFalse(auth.has_role(None, auth.ROLES))
        with patch("app.auth.time.time", return_value=1_000 + auth.SESSION_TTL_SECONDS):
            self.assertIsNone(auth.get_session(token))
        self.assertIsNone(auth.get_session("untrusted"))

    def test_brute_force_limit_persists_and_expires(self) -> None:
        self.admin()
        with patch("app.auth.time.time", return_value=1_000):
            for _ in range(auth.LOGIN_MAX_FAILURES):
                self.assertIsNone(auth.authenticate("admin", "incorrect-password"))
            auth.init_db()  # An application restart must not erase the throttle.
            with self.assertRaises(auth.LoginRateLimited) as error:
                auth.authenticate("ADMIN", "a-long-test-password")
            self.assertEqual(error.exception.retry_after, auth.LOGIN_LOCK_SECONDS)
        with patch("app.auth.time.time", return_value=1_001 + auth.LOGIN_LOCK_SECONDS):
            self.assertIsNotNone(auth.authenticate("admin", "a-long-test-password"))

    def test_client_limit_covers_unknown_accounts(self) -> None:
        with patch("app.auth.time.time", return_value=1_000):
            for i in range(auth.LOGIN_MAX_FAILURES):
                self.assertIsNone(auth.authenticate(f"unknown{i}", "incorrect-password", client_id="127.0.0.1"))
            with self.assertRaises(auth.LoginRateLimited):
                auth.authenticate("another-user", "incorrect-password", client_id="127.0.0.1")

    def test_optional_email_is_trimmed_validated_and_separate_from_login(self) -> None:
        admin = auth.create_initial_admin("Admin", "Администратор", "a-long-test-password",
                                          "  admin@SAMRUK-DEMO.test  ")
        self.assertEqual(admin["email"], "admin@samruk-demo.test")
        user = auth.create_user("reader", "Читатель", "a-long-test-password", "viewer",
                                "  reader+demo@samruk-demo.test ")
        self.assertEqual(user["email"], "reader+demo@samruk-demo.test")
        self.assertIsNone(auth.authenticate(user["email"], "a-long-test-password"))
        token, _, signed_in = auth.authenticate("reader", "a-long-test-password")
        self.assertEqual(signed_in["email"], user["email"])
        self.assertEqual(auth.get_session(token)["user"]["username"], "reader")
        self.assertIsNone(auth.create_user("empty", "Без почты", "a-long-test-password", "viewer", "  ")["email"])
        self.assertIsNone(auth.create_user("missing", "Без почты", "a-long-test-password", "viewer")["email"])
        for invalid in ("not-an-email", "a@b", "a@@b.test", "a b@c.test", ".a@c.test", "a..b@c.test",
                        "a@-c.test", "a@c..test", "a@c.test\nInjected: yes", 42):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "email"):
                auth.create_user("invalid", "Name", "a-long-test-password", "viewer", invalid)

    def test_legacy_email_migration_preserves_credentials_roles_and_sessions(self) -> None:
        legacy_db = Path(self.directory.name) / "legacy.sqlite3"
        password_hash = auth._hash_password("legacy-long-password")
        token = "a" * 43
        with sqlite3.connect(legacy_db) as connection:
            connection.executescript("""
                CREATE TABLE auth_users (username TEXT PRIMARY KEY, display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL, role TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE auth_sessions (token_hash TEXT PRIMARY KEY, username TEXT NOT NULL,
                    csrf_token TEXT NOT NULL, expires_at REAL NOT NULL);
            """)
            connection.execute("INSERT INTO auth_users VALUES(?,?,?,?,?)",
                               ("legacy.admin", "Прежний администратор", password_hash, "admin", "2026-01-01"))
            connection.execute("INSERT INTO auth_sessions VALUES(?,?,?,?)",
                               (auth._token_hash(token), "legacy.admin", "legacy-csrf", 9_999_999_999))
        with patch.object(store, "DB_PATH", legacy_db):
            auth.init_db()
            auth.init_db()
            with store._connect() as connection:
                row = dict(connection.execute("SELECT * FROM auth_users").fetchone())
            self.assertEqual(row, {"username": "legacy.admin", "display_name": "Прежний администратор",
                                  "password_hash": password_hash, "role": "admin", "created_at": "2026-01-01",
                                  "email": None})
            self.assertEqual(auth.get_session(token)["csrf"], "legacy-csrf")
            self.assertIsNotNone(auth.authenticate("legacy.admin", "legacy-long-password"))


if __name__ == "__main__":
    unittest.main()
