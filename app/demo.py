"""Explicit, repeatable local fixtures; importing the module never seeds accounts.

Run ``python -m app.demo --seed`` after first-run administrator setup. These
hand-authored examples exercise review and notifications, not speech recognition.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import uuid
from datetime import date, timedelta
from typing import Any


PROFILES = (
    {"username": "a.saparova", "display_name": "Айгерим Сапарова", "role": "secretary",
     "email": "a.saparova@samruk-demo.test"},
    {"username": "d.omarov", "display_name": "Данияр Омаров", "role": "viewer",
     "email": "d.omarov@samruk-demo.test"},
    {"username": "m.serikova", "display_name": "Мадина Серикова", "role": "viewer",
     "email": "m.serikova@samruk-demo.test"},
)
FIXTURE_VERSION = "local-inbox-demo-v1"
APPROVED_ID = uuid.uuid5(uuid.NAMESPACE_URL, FIXTURE_VERSION + "/approved").hex
REVIEW_ID = uuid.uuid5(uuid.NAMESPACE_URL, FIXTURE_VERSION + "/review").hex


def _task(item_id: str, title: str, recipient: dict[str, str], due: date | None,
          segment_id: str, *, status: str = "in_progress") -> dict[str, Any]:
    return {"id": item_id, "kind": "action", "title": title,
            "description": "Учебное поручение для проверки интерфейса и внутренних уведомлений.",
            "owner": recipient["display_name"], "due_text": due.isoformat() if due else None,
            "due_date": due.isoformat() if due else None, "status": status,
            "source_segment_ids": [segment_id], "review_note": "ДЕМО: введено вручную, без AI-анализа.",
            "reminder_recipient": recipient["username"], "reminder_channel": "inbox"}


def _fixtures(today: date) -> list[dict[str, Any]]:
    secretary, daniyar, madina = PROFILES
    approved_tasks = [
        _task("demo-new", "Подготовить перечень вопросов к следующей встрече", daniyar, None, "1"),
        _task("demo-soon", "Согласовать план закупок", daniyar, today + timedelta(days=2), "2"),
        _task("demo-overdue", "Передать отчёт о ходе проекта", daniyar, today - timedelta(days=1), "3"),
        _task("demo-madina", "Проверить комплектность материалов", madina, None, "4"),
    ]
    review_tasks = [
        _task("demo-review", "Подготовить материалы для следующего обсуждения", daniyar, None, "1",
              status="draft")
    ]
    summary = (
        "ДЕМО: учебный текстовый пример, подготовленный вручную. Аудиозаписи нет; "
        "распознавание речи и AI-анализ для этих данных не выполнялись. Участники распределили "
        "поручения, чтобы показать проверку протокола, выбор получателя и внутренние уведомления. "
        "Адреса в зоне .test служат только примером контактного поля, письма не отправляются."
    )
    fixtures = []
    for meeting_id, title, state, items in (
        (APPROVED_ID, "[ДЕМО] Поручения и личные уведомления", "approved", approved_tasks),
        (REVIEW_ID, "[ДЕМО] Протокол для проверки секретарём", "review", review_tasks),
    ):
        segments = [
            {"id": str(index), "start": 0.0, "end": 0.0, "speaker_id": "S1", "confidence": None,
             "text": f"{item['owner']}: {item['title']}. "
                     + (f"Срок: {item['due_date']}." if item["due_date"] else "Срок не установлен.")}
            for index, item in enumerate(items, 1)
        ]
        fixtures.append({"id": meeting_id, "title": title, "state": state, "items": items,
                         "summary": summary, "segments": segments,
                         "speakers": {"S1": secretary["display_name"]},
                         "participants": [person["display_name"] for person in PROFILES]})
    return fixtures


def seed_demo() -> dict[str, Any]:
    """Add missing fixtures atomically and return passwords only for newly created accounts.

    Existing matching accounts and any edited demo content are left untouched.
    A profile or meeting-ID collision aborts the entire seed before data changes.
    """
    from . import auth, reminders, store

    auth.init_db()
    reminders.init_db()
    today = reminders.local_today()
    fixtures = _fixtures(today)
    credentials: list[dict[str, str]] = []
    created_meetings: list[str] = []
    with store._LOCK, store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if not connection.execute("SELECT 1 FROM auth_users WHERE role='admin' LIMIT 1").fetchone():
            raise ValueError("Сначала создайте администратора через экран первоначальной настройки приложения.")
        existing_users = {}
        for profile in PROFILES:
            row = connection.execute("SELECT * FROM auth_users WHERE username=?", (profile["username"],)).fetchone()
            if row is not None and any(row[key] != profile[key] for key in ("display_name", "role", "email")):
                raise ValueError(f"Логин {profile['username']} уже занят другим профилем. Демо не создано.")
            existing_users[profile["username"]] = row
        existing_meetings = set()
        for fixture in fixtures:
            row = connection.execute("SELECT source FROM meetings WHERE id=?", (fixture["id"],)).fetchone()
            if row is None:
                continue
            marker = connection.execute(
                "SELECT details_json FROM audit_events WHERE meeting_id=? AND event_type='demo_seeded'",
                (fixture["id"],),
            ).fetchall()
            if row["source"] != "demo" or not any(
                json.loads(event["details_json"]).get("fixture_version") == FIXTURE_VERSION for event in marker
            ):
                raise ValueError("Идентификатор учебного протокола уже занят. Демо не создано.")
            existing_meetings.add(fixture["id"])
        timestamp = store.now()
        for profile in PROFILES:
            if existing_users[profile["username"]] is not None:
                continue
            password = secrets.token_urlsafe(24)
            connection.execute(
                "INSERT INTO auth_users(username,display_name,password_hash,role,created_at,email) VALUES(?,?,?,?,?,?)",
                (profile["username"], profile["display_name"], auth._hash_password(password),
                 profile["role"], timestamp, auth._normalize_email(profile["email"])),
            )
            credentials.append({"username": profile["username"], "password": password})
        for fixture in fixtures:
            if fixture["id"] in existing_meetings:
                continue
            connection.execute(
                """INSERT INTO meetings
                (id,title,meeting_date,source,provider,participants_json,media_path,state,stage,
                 summary,segments_json,items_json,speakers_json,created_at,updated_at,approved_at)
                VALUES(?,?,?,'demo',NULL,?,?,?,?,?,?,?,?,?,?,?)""",
                (fixture["id"], fixture["title"], today.isoformat(),
                 json.dumps(fixture["participants"], ensure_ascii=False),
                 str(store.DATA_DIR / fixture["id"] / "source.wav"), fixture["state"],
                 "Учебный пример без аудиозаписи · " + ("Утверждён" if fixture["state"] == "approved"
                                                       else "Ожидает проверки секретарём"),
                 fixture["summary"], json.dumps(fixture["segments"], ensure_ascii=False),
                 json.dumps(fixture["items"], ensure_ascii=False),
                 json.dumps(fixture["speakers"], ensure_ascii=False), timestamp, timestamp,
                 timestamp if fixture["state"] == "approved" else None),
            )
            connection.execute(
                "INSERT INTO audit_events(meeting_id,actor,event_type,details_json,created_at) VALUES(?,?,'demo_seeded',?,?)",
                (fixture["id"], "demo-cli", json.dumps({"fixture_version": FIXTURE_VERSION,
                 "manual_fixture": True, "ai_processed": False}), timestamp),
            )
            created_meetings.append(fixture["id"])
    return {"new_accounts": credentials, "new_meetings": created_meetings, "today": today.isoformat()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Явное добавление локальных учебных аккаунтов и протоколов.")
    parser.add_argument("--seed", action="store_true", help="Создать отсутствующие демоданные без сброса существующих")
    args = parser.parse_args(argv)
    if not args.seed:
        parser.error("для добавления демоданных явно укажите --seed")
    try:
        result = seed_demo()
    except ValueError as error:
        print(f"Демо не создано: {error}", file=sys.stderr)
        return 1
    if result["new_accounts"]:
        print("Пароли только новых учебных аккаунтов. Сохраните сейчас: повторно они не выводятся.")
        for account in result["new_accounts"]:
            print(f"{account['username']}\t{account['password']}")
        sys.stdout.flush()
    else:
        print("Новых аккаунтов нет. Существующие пароли и роли сохранены.")
    # Print credentials before this independent step so a notification failure
    # cannot hide the only copy of a newly generated account password.
    from . import reminders

    created = reminders.run_once()
    print(f"Добавлено учебных протоколов: {len(result['new_meetings'])}; новых уведомлений: {created}.")
    print("ДЕМО: ручные текстовые примеры без аудио и AI-обработки. Email используется только как контакт.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
