"""Local accounts and revocable sessions for the shared organization archive.

The HTTP layer enforces roles and sends the session token in an HttpOnly cookie.
Only a SHA-256 digest of that opaque token is kept in SQLite. No default account
or password exists: create_initial_admin() performs the first-run setup once.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import math
import os
import re
import secrets
import time
from typing import Any, Iterable

from . import store


ROLES = frozenset({"admin", "secretary", "viewer"})
PASSWORD_ITERATIONS = 600_000
SESSION_TTL_SECONDS = 12 * 60 * 60
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_LOCK_SECONDS = 15 * 60
_USERNAME = re.compile(r"[a-z0-9][a-z0-9_.-]{2,63}\Z", re.ASCII)
_EMAIL_LOCAL = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+\Z", re.ASCII)
_EMAIL_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z", re.ASCII)
_DUMMY_SALT = b"ai-protocolist-dummy-password"


class LoginRateLimited(ValueError):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = max(1, retry_after)
        super().__init__("Слишком много попыток входа. Повторите позже.")


def init_db() -> None:
    with store._connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS auth_users (
                username TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin', 'secretary', 'viewer')),
                created_at TEXT NOT NULL,
                email TEXT
            );
            CREATE TABLE IF NOT EXISTS auth_sessions (
                token_hash TEXT PRIMARY KEY,
                username TEXT NOT NULL REFERENCES auth_users(username) ON DELETE CASCADE,
                csrf_token TEXT NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS auth_sessions_expiry ON auth_sessions(expires_at);
            CREATE TABLE IF NOT EXISTS auth_login_attempts (
                attempt_key TEXT PRIMARY KEY,
                failure_count INTEGER NOT NULL,
                window_start REAL NOT NULL,
                blocked_until REAL NOT NULL DEFAULT 0
            );
            """
        )
        # Add only the optional contact field; existing logins, hashes and sessions survive.
        connection.execute("BEGIN IMMEDIATE")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(auth_users)")}
        if "email" not in columns:
            connection.execute("ALTER TABLE auth_users ADD COLUMN email TEXT")
        connection.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (time.time(),))


def _public_user(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in ("username", "display_name", "role", "created_at", "email")}


def _normalize_username(username: str) -> str:
    value = username.strip().lower()
    if not _USERNAME.fullmatch(value):
        raise ValueError("Логин: от 3 до 64 латинских букв, цифр и символов . _ -")
    return value


def _normalize_email(email: str | None) -> str | None:
    """Validate an optional contact address; it is never used as a login or delivery channel."""
    if email is None:
        return None
    if not isinstance(email, str):
        raise ValueError("Укажите корректный email")
    value = email.strip()
    if not value:
        return None
    local, separator, domain = value.rpartition("@")
    labels = domain.split(".")
    if (len(value) > 254 or not separator or not 1 <= len(local) <= 64
            or not _EMAIL_LOCAL.fullmatch(local) or local.startswith(".") or local.endswith(".")
            or ".." in local or len(labels) < 2
            or any(not _EMAIL_LABEL.fullmatch(label) for label in labels)
            or not re.fullmatch(r"[A-Za-z]{2,63}", labels[-1], re.ASCII)):
        raise ValueError("Укажите корректный email")
    return f"{local}@{domain.lower()}"


def _hash_password(password: str) -> str:
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise ValueError("Пароль должен содержать от 12 до 256 символов")
    salt = secrets.token_bytes(24)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return "$".join(("pbkdf2_sha256", str(PASSWORD_ITERATIONS),
                     base64.b64encode(salt).decode("ascii"), base64.b64encode(digest).decode("ascii")))


def _verify_password(password: str, encoded: str | None) -> bool:
    valid_input = isinstance(password, str) and 12 <= len(password) <= 256
    candidate = password if valid_input else "invalid-password-input"
    salt, iterations, expected = _DUMMY_SALT, PASSWORD_ITERATIONS, b"\0" * 32
    valid_hash = False
    if encoded:
        try:
            algorithm, rounds, salt64, digest64 = encoded.split("$")
            iterations = int(rounds)
            if algorithm != "pbkdf2_sha256" or not 1_000 <= iterations <= 2_000_000:
                raise ValueError("Unsupported password encoding")
            salt = base64.b64decode(salt64, validate=True)
            expected = base64.b64decode(digest64, validate=True)
            valid_hash = len(salt) >= 16 and len(expected) == 32
        except (ValueError, TypeError):
            salt, iterations, expected = _DUMMY_SALT, PASSWORD_ITERATIONS, b"\0" * 32
    actual = hashlib.pbkdf2_hmac("sha256", candidate.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected) and valid_input and valid_hash


def is_configured() -> bool:
    with store._connect() as connection:
        return connection.execute("SELECT 1 FROM auth_users LIMIT 1").fetchone() is not None


def _create_user(username: str, display_name: str, password: str, role: str,
                 email: str | None = None, *, first_admin: bool = False) -> dict[str, Any]:
    username = _normalize_username(username)
    display_name = display_name.strip()
    if not display_name or len(display_name) > 120:
        raise ValueError("Укажите имя длиной до 120 символов")
    if role not in ROLES:
        raise ValueError("Неизвестная роль пользователя")
    email = _normalize_email(email)
    password_hash = _hash_password(password)
    user = {"username": username, "display_name": display_name, "role": role,
            "created_at": store.now(), "email": email}
    with store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if first_admin and connection.execute("SELECT 1 FROM auth_users LIMIT 1").fetchone():
            raise ValueError("Первичная настройка уже выполнена")
        if connection.execute("SELECT 1 FROM auth_users WHERE username=?", (username,)).fetchone():
            raise ValueError("Такой логин уже существует")
        connection.execute(
            "INSERT INTO auth_users(username,display_name,password_hash,role,created_at,email) VALUES(?,?,?,?,?,?)",
            (username, display_name, password_hash, role, user["created_at"], email),
        )
    return user


def create_initial_admin(username: str, display_name: str, password: str,
                         email: str | None = None) -> dict[str, Any]:
    """Atomic bootstrap; it is impossible to replace an existing installation."""
    return _create_user(username, display_name, password, "admin", email, first_admin=True)


def create_user(username: str, display_name: str, password: str, role: str,
                email: str | None = None) -> dict[str, Any]:
    """Create an account. The caller must require an authenticated administrator."""
    return _create_user(username, display_name, password, role, email)


def list_users() -> list[dict[str, Any]]:
    with store._connect() as connection:
        return [_public_user(row) for row in connection.execute("SELECT * FROM auth_users ORDER BY username")]


def demo_users() -> list[dict[str, Any]]:
    """Expose only the three seeded fictional identities in explicitly enabled demo mode."""
    if os.getenv("PROTOCOL_DEMO_MODE") != "1":
        return []
    from .demo import APPROVED_ID, FIXTURE_VERSION, PROFILES
    import json

    with store._connect() as connection:
        markers = connection.execute(
            "SELECT details_json FROM audit_events WHERE meeting_id=? AND event_type='demo_seeded'",
            (APPROVED_ID,),
        ).fetchall()
        if not any(json.loads(marker["details_json"]).get("fixture_version") == FIXTURE_VERSION
                   for marker in markers):
            return []
        users = []
        for profile in PROFILES:
            row = connection.execute("SELECT * FROM auth_users WHERE username=?", (profile["username"],)).fetchone()
            if row is None or any(row[key] != value for key, value in profile.items()):
                return []
            users.append(_public_user(row))
        return users


def authenticate_demo(username: str) -> tuple[str, str, dict[str, Any]] | None:
    """Issue a regular revocable session for a fixed demo persona, never an administrator."""
    user = next((user for user in demo_users() if user["username"] == username), None)
    if user is None:
        return None
    current = time.time()
    token, csrf_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with store._connect() as connection:
        connection.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (current,))
        connection.execute("INSERT INTO auth_sessions(token_hash,username,csrf_token,expires_at) VALUES(?,?,?,?)",
                           (_token_hash(token), username, csrf_token, current + SESSION_TTL_SECONDS))
    return token, csrf_token, user


def _attempt_keys(username: str, client_id: str | None) -> list[str]:
    # Hash identifiers to bound database key size. Account throttles apply even
    # without a client id and cannot be bypassed by switching source addresses.
    keys = ["account:" + hashlib.sha256(username.encode("utf-8")).hexdigest()]
    if client_id:
        keys.append("client:" + hashlib.sha256(client_id.encode("utf-8")).hexdigest())
    return keys


def _check_limits(connection: Any, keys: list[str], current: float) -> None:
    for key in keys:
        row = connection.execute("SELECT blocked_until FROM auth_login_attempts WHERE attempt_key=?", (key,)).fetchone()
        if row and row["blocked_until"] > current:
            raise LoginRateLimited(math.ceil(row["blocked_until"] - current))


def authenticate(username: str, password: str, *, client_id: str | None = None
                 ) -> tuple[str, str, dict[str, Any]] | None:
    """Return (session token, CSRF token, public user), None, or LoginRateLimited.

    Supply the socket peer address as client_id, never an untrusted forwarded
    header. Limits are persisted and shared between server workers.
    """
    normalized = username.strip().lower()
    keys = _attempt_keys(normalized, client_id)
    with store._connect() as connection:
        _check_limits(connection, keys, time.time())
        user = connection.execute("SELECT * FROM auth_users WHERE username=?", (normalized,)).fetchone()
    valid = _verify_password(password, user["password_hash"] if user else None)
    current = time.time()
    with store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        # Check again under the write lock so concurrent failures cannot bypass it.
        _check_limits(connection, keys, current)
        connection.execute("DELETE FROM auth_login_attempts WHERE window_start < ? AND blocked_until <= ?",
                           (current - 24 * 60 * 60, current))
        if not valid or user is None:
            for key in keys:
                row = connection.execute("SELECT * FROM auth_login_attempts WHERE attempt_key=?", (key,)).fetchone()
                in_window = row is not None and current - row["window_start"] < LOGIN_WINDOW_SECONDS
                count = row["failure_count"] + 1 if in_window else 1
                start = row["window_start"] if in_window else current
                blocked_until = current + LOGIN_LOCK_SECONDS if count >= LOGIN_MAX_FAILURES else 0
                connection.execute(
                    "INSERT OR REPLACE INTO auth_login_attempts VALUES(?,?,?,?)", (key, count, start, blocked_until))
            return None
        connection.execute("DELETE FROM auth_login_attempts WHERE attempt_key=?", (keys[0],))
        connection.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (current,))
        token, csrf_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        connection.execute("INSERT INTO auth_sessions(token_hash,username,csrf_token,expires_at) VALUES(?,?,?,?)",
                           (_token_hash(token), user["username"], csrf_token, current + SESSION_TTL_SECONDS))
    return token, csrf_token, _public_user(user)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def get_session(token: str | None) -> dict[str, Any] | None:
    if not token or not 32 <= len(token) <= 128:
        return None
    with store._connect() as connection:
        row = connection.execute(
            """SELECT u.*, s.csrf_token FROM auth_sessions s JOIN auth_users u USING(username)
               WHERE s.token_hash=? AND s.expires_at > ?""", (_token_hash(token), time.time())).fetchone()
    return {"user": _public_user(row), "csrf": row["csrf_token"]} if row else None


def logout(token: str | None) -> None:
    if token and 32 <= len(token) <= 128:
        with store._connect() as connection:
            connection.execute("DELETE FROM auth_sessions WHERE token_hash=?", (_token_hash(token),))


def has_role(user: dict[str, Any] | None, allowed_roles: Iterable[str]) -> bool:
    return bool(user and user.get("role") in allowed_roles)


def validate_csrf(session: dict[str, Any], supplied: str | None) -> bool:
    expected = session.get("csrf")
    return bool(isinstance(expected, str) and isinstance(supplied, str)
                and hmac.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8")))
