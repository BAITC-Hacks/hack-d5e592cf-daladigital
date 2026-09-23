"""Local-only API and the secretary's review interface."""

from __future__ import annotations

import os
import importlib.util
import re
import shutil
from contextlib import asynccontextmanager
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, check_ready, exports, recording_notice, reminders, service, store
from .config import MODEL_DIR
from .model_files import ASR_BYTES, SPEAKER_BYTES, available


MAX_MEDIA_BYTES = int(os.getenv("PROTOCOL_MAX_MEDIA_BYTES", str(500 * 1024 * 1024)))
SUFFIXES = {".mp3", ".wav", ".m4a", ".mp4", ".webm", ".ogg"}
STATIC = Path(__file__).with_name("static")
@asynccontextmanager
async def lifespan(_app: FastAPI):
    auth.init_db()
    reminders.init_db()
    scheduler = reminders.ReminderScheduler()
    scheduler.start()
    try:
        yield
    finally:
        scheduler.stop()


app = FastAPI(title="AI Протоколист", version="0.2.0", docs_url="/api/docs", redoc_url=None,
              lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

COOKIE_NAME = "protocol_demo_session" if os.getenv("PROTOCOL_DEMO_MODE") == "1" else "protocol_session"
PUBLIC_API = {"/api/auth/status", "/api/auth/setup", "/api/auth/login", "/api/auth/demo-login"}


@app.middleware("http")
async def access_control(request: Request, call_next):
    path = request.url.path
    protected = (path.startswith("/api/") or path == "/openapi.json") and path not in PUBLIC_API
    mutation = request.method not in {"GET", "HEAD", "OPTIONS"}
    if mutation:
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "Запрос с другого сайта запрещён"}, status_code=403)
    if protected:
        session = auth.get_session(request.cookies.get(COOKIE_NAME, ""))
        if not session:
            return JSONResponse({"detail": "Войдите в приложение"}, status_code=401)
        request.state.user = session["user"]
        request.state.csrf = session["csrf"]
        if mutation:
            if not auth.validate_csrf(session, request.headers.get("x-csrf-token", "")):
                return JSONResponse({"detail": "Обновите страницу и повторите действие"}, status_code=403)
            personal = path == "/api/auth/logout" or bool(re.fullmatch(r"/api/notifications/\d+/read", path))
            if not personal and session["user"]["role"] == "viewer":
                return JSONResponse({"detail": "Эта роль разрешает только просмотр"}, status_code=403)
            if (path == "/api/users" or request.method == "DELETE") and session["user"]["role"] != "admin":
                return JSONResponse({"detail": "Действие доступно администратору"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "media-src 'self' blob:; img-src 'self' data:; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class UserBody(LoginBody):
    display_name: str = Field(min_length=1, max_length=100)
    role: Literal["admin", "secretary", "viewer"] = "viewer"
    email: str | None = Field(default=None, max_length=254)


def _login_response(username: str, password: str, request: Request) -> JSONResponse:
    try:
        result = auth.authenticate(username, password, client_id=request.client.host if request.client else None)
    except auth.LoginRateLimited as exc:
        raise HTTPException(429, "Слишком много попыток. Повторите вход позже.",
                            headers={"Retry-After": str(exc.retry_after)}) from exc
    if result is None:
        raise HTTPException(401, "Неверный логин или пароль")
    return _session_response(result)


def _session_response(result: tuple) -> JSONResponse:
    token, csrf, user = result
    response = JSONResponse({"user": user, "csrf": csrf})
    response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="strict", max_age=auth.SESSION_TTL_SECONDS,
                        secure=os.getenv("PROTOCOL_SECURE_COOKIE", "0") == "1")
    return response


@app.get("/api/auth/status")
def auth_status(request: Request) -> dict:
    local = request.client and request.client.host in {"127.0.0.1", "::1", "testclient"}
    profiles = auth.demo_users() if local else []
    return {"configured": auth.is_configured(), "demo": bool(profiles), "demo_users": profiles,
            "demo_default": "d.omarov" if profiles else None}


class DemoLogin(BaseModel):
    username: str = Field(min_length=1, max_length=64)


@app.post("/api/auth/demo-login")
def demo_login(body: DemoLogin, request: Request):
    if os.getenv("PROTOCOL_DEMO_MODE") != "1":
        raise HTTPException(404, "Деморежим не включён")
    if not request.client or request.client.host not in {"127.0.0.1", "::1", "testclient"}:
        raise HTTPException(403, "Демо доступно только на этом компьютере")
    result = auth.authenticate_demo(body.username)
    if result is None:
        raise HTTPException(403, "Выберите подготовленного демо-сотрудника")
    auth.logout(request.cookies.get(COOKIE_NAME, ""))
    return _session_response(result)


@app.post("/api/auth/setup", status_code=201)
def setup(body: UserBody, request: Request):
    if not request.client or request.client.host not in {"127.0.0.1", "::1", "testclient"}:
        raise HTTPException(403, "Первую учётную запись создайте на компьютере сервера")
    try:
        auth.create_initial_admin(body.username, body.display_name, body.password, email=body.email)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _login_response(body.username, body.password, request)


@app.post("/api/auth/login")
def login(body: LoginBody, request: Request):
    return _login_response(body.username, body.password, request)


@app.get("/api/auth/me")
def me(request: Request) -> dict:
    return {"user": request.state.user, "csrf": request.state.csrf}


@app.post("/api/auth/logout")
def logout(request: Request):
    auth.logout(request.cookies.get(COOKIE_NAME, ""))
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE_NAME)
    return response


@app.get("/api/users")
def users(request: Request) -> list[dict]:
    if request.state.user["role"] == "viewer":
        raise HTTPException(403, "Список получателей доступен секретарю и администратору")
    return auth.list_users()


@app.post("/api/users", status_code=201)
def add_user(body: UserBody) -> dict:
    try:
        return auth.create_user(body.username, body.display_name, body.password, body.role, email=body.email)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/notifications")
def notifications(request: Request) -> list[dict]:
    return reminders.list_notifications(request.state.user["username"])


@app.post("/api/notifications/{notification_id}/read")
def read_notification(notification_id: int, request: Request) -> dict:
    if not reminders.mark_read(request.state.user["username"], notification_id):
        raise HTTPException(404, "Напоминание не найдено")
    return {"ok": True}


def _validate_reminder(changes: dict) -> None:
    for key in ("due_date", "reminder_recipient"):
        if changes.get(key) is not None and not isinstance(changes[key], str):
            raise HTTPException(422, "Срок и получатель должны быть строками")
    if changes.get("due_date") is not None:
        _validated_date(changes["due_date"])
    if changes.get("reminder_channel", "inbox") != "inbox":
        raise HTTPException(422, "Поддерживаются уведомления внутри приложения")
    recipient = changes.get("reminder_recipient")
    if recipient and recipient not in {user["username"] for user in auth.list_users()}:
        raise HTTPException(422, "Выберите существующего получателя")


def _meeting(meeting_id: str) -> dict:
    try:
        return store.get_meeting(meeting_id)
    except KeyError as exc:
        raise HTTPException(404, "Совещание не найдено") from exc


def _validated_date(value: str) -> str:
    try:
        normalized = date.fromisoformat(value).isoformat()
        if value != normalized:
            raise ValueError("Noncanonical date")
        return normalized
    except ValueError as exc:
        raise HTTPException(422, "Нужна дата в формате YYYY-MM-DD") from exc


def _participants(value: str) -> list[str]:
    return [part.strip()[:100] for part in re.split(r"[,;\n]", value) if part.strip()][:30]


def _error(exc: Exception) -> HTTPException:
    return HTTPException(409, str(exc))


class StartRecording(BaseModel):
    title: str = Field(min_length=2, max_length=200)
    meeting_date: str
    participants: str = ""
    source: Literal["room", "platform"]
    provider: Literal["meet", "zoom", "teams"] | None = None
    meeting_url: str | None = None
    consent_confirmed: bool = False
    notice_version: str = ""


class FinishRecording(BaseModel):
    gaps: list[dict] = Field(default_factory=list)


class ItemEdit(BaseModel):
    actor: str = Field(min_length=1, max_length=100)
    changes: dict


class ItemCreate(BaseModel):
    actor: str = Field(min_length=1, max_length=100)
    kind: Literal["action", "decision", "initiative", "question", "risk"]
    title: str = Field(min_length=2, max_length=500)
    description: str = ""
    owner: str | None = None
    due_text: str | None = None
    due_date: str | None = None
    source_segment_ids: list[str]
    review_note: str = "Добавлено секретарём"
    reminder_recipient: str | None = None
    reminder_channel: Literal["inbox"] = "inbox"


class SummaryEdit(BaseModel):
    actor: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=10000)


class SpeakerEdit(BaseModel):
    actor: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)


class Approval(BaseModel):
    actor: str = Field(min_length=1, max_length=100)
    confirmed: bool


def _validate_meeting_url(provider: str, url: str | None) -> None:
    if not url:
        raise HTTPException(422, "Укажите ссылку на встречу")
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    valid = {
        "meet": host == "meet.google.com",
        "zoom": host == "zoom.us" or host.endswith(".zoom.us"),
        "teams": host in {"teams.microsoft.com", "teams.live.com"},
    }
    if parsed.scheme != "https" or not valid.get(provider, False):
        raise HTTPException(422, "Ссылка не соответствует выбранной платформе")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health() -> dict:
    readiness = check_ready.check()
    return {"status": "ok" if readiness["ready"] else "needs_setup", **readiness,
            "offline_runtime": True, "ffmpeg": shutil.which("ffmpeg") is not None,
            "asr_package": importlib.util.find_spec("faster_whisper") is not None,
            "asr_model": available(MODEL_DIR / "whisper-large-v3-turbo" / "model.bin", ASR_BYTES),
            "diarize_package": importlib.util.find_spec("diarize") is not None,
            "diarize_model": available(MODEL_DIR / "wespeaker" / "model.onnx", SPEAKER_BYTES),
            "ollama_executable": shutil.which("ollama") is not None,
            "ollama_model": os.getenv("PROTOCOL_OLLAMA_MODEL", "qwen3:4b")}


@app.get("/api/meetings")
def meetings() -> list[dict]:
    return store.list_meetings()


@app.get("/api/meetings/{meeting_id}")
def meeting(meeting_id: str) -> dict:
    return _meeting(meeting_id)


class DeleteMeeting(BaseModel):
    confirm_title: str


@app.delete("/api/meetings/{meeting_id}")
def delete_meeting(meeting_id: str, body: DeleteMeeting, request: Request) -> dict:
    entry = _meeting(meeting_id)
    if body.confirm_title != entry["title"]:
        raise HTTPException(422, "Введите точное название совещания для удаления")
    try:
        store.delete_meeting(meeting_id, request.state.user["username"])
    except ValueError as exc:
        raise _error(exc) from exc
    return {"deleted": True}


@app.get("/api/dashboard")
def dashboard() -> dict:
    return service.dashboard()


@app.post("/api/meetings/upload", status_code=201)
async def upload(title: str = Form(...), meeting_date: str = Form(...), participants: str = Form(""),
                 file: UploadFile = File(...)) -> dict:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in SUFFIXES:
        raise HTTPException(415, "Поддерживаются MP3, WAV, M4A, MP4, WebM и OGG")
    if not 2 <= len(title.strip()) <= 200:
        raise HTTPException(422, "Название: 2–200 символов")
    entry = store.create_meeting(title=title, meeting_date=_validated_date(meeting_date),
                                 source="upload", provider=None, participants=_participants(participants),
                                 suffix=suffix)
    path = Path(entry["media_path"])
    size = 0
    try:
        with path.open("xb") as target:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_MEDIA_BYTES:
                    raise HTTPException(413, "Запись превышает допустимый размер")
                target.write(chunk)
        if size < 1000:
            raise HTTPException(422, "Запись пуста")
        return service.enqueue(entry["id"])
    except Exception:
        path.unlink(missing_ok=True)
        store.update_meeting(entry["id"], state="error", stage="Ошибка загрузки", error="Файл не сохранён")
        raise
    finally:
        await file.close()


@app.get("/api/recording-notice")
def get_recording_notice() -> dict:
    return recording_notice.get_notice()


@app.post("/api/meetings/live", status_code=201)
def start_recording(body: StartRecording, request: Request) -> dict:
    if not body.consent_confirmed:
        raise HTTPException(422, "Подтвердите, что вы уведомили участников о записи")
    if body.notice_version != recording_notice.VERSION:
        raise HTTPException(422, "Обновите предупреждение о записи и подтвердите уведомление участников")
    if body.source == "platform":
        if not body.provider:
            raise HTTPException(422, "Выберите платформу")
        _validate_meeting_url(body.provider, body.meeting_url)
    elif body.provider or body.meeting_url:
        raise HTTPException(422, "Для записи в переговорной платформа не нужна")
    entry = store.create_meeting(title=body.title, meeting_date=_validated_date(body.meeting_date),
                                 source=body.source, provider=body.provider,
                                 participants=_participants(body.participants), suffix=".webm")
    store.audit(entry["id"], request.state.user["username"], "recording_notice_acknowledged",
                {**recording_notice.get_notice(), "confirmation": "secretary_acknowledgement"})
    return entry


@app.post("/api/meetings/{meeting_id}/chunks")
async def append_chunk(meeting_id: str, request: Request) -> dict:
    entry = _meeting(meeting_id)
    if entry["state"] != "recording":
        raise HTTPException(409, "Запись уже остановлена")
    data = await request.body()
    if not data or len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, "Некорректный фрагмент записи")
    path = Path(entry["media_path"])
    size = path.stat().st_size if path.exists() else 0
    if size + len(data) > MAX_MEDIA_BYTES:
        raise HTTPException(413, "Достигнут предел размера записи")
    with path.open("ab") as target:
        target.write(data)
    return {"bytes_saved": size + len(data)}


@app.post("/api/meetings/{meeting_id}/finish")
def finish_recording(meeting_id: str, body: FinishRecording) -> dict:
    entry = _meeting(meeting_id)
    if entry["state"] != "recording":
        raise HTTPException(409, "Запись уже остановлена")
    gaps = [{"start": max(0, float(g.get("start", 0))), "end": max(0, float(g.get("end", 0))),
             "reason": str(g.get("reason", "Нет сигнала"))[:100]} for g in body.gaps[:100]]
    store.update_meeting(meeting_id, gaps=gaps)
    try:
        return service.enqueue(meeting_id)
    except ValueError as exc:
        raise _error(exc) from exc


@app.post("/api/meetings/{meeting_id}/retry")
def retry(meeting_id: str) -> dict:
    entry = _meeting(meeting_id)
    if entry["state"] != "error":
        raise HTTPException(409, "Повтор доступен после ошибки")
    try:
        return service.enqueue(meeting_id)
    except ValueError as exc:
        raise _error(exc) from exc


@app.post("/api/meetings/{meeting_id}/reanalyze", status_code=201)
def reanalyze(meeting_id: str) -> dict:
    _meeting(meeting_id)
    try:
        return service.reanalyze(meeting_id)
    except ValueError as exc:
        raise _error(exc) from exc


@app.patch("/api/meetings/{meeting_id}/items/{item_id}")
def edit_item(meeting_id: str, item_id: str, body: ItemEdit, request: Request) -> dict:
    entry = _meeting(meeting_id)
    original = next((item for item in entry["items"] if item["id"] == item_id), {})
    _validate_reminder({**original, **body.changes})
    if "status" in body.changes and body.changes["status"] not in {"draft", "in_progress", "done", "cancelled"}:
        raise HTTPException(422, "Недопустимый статус")
    try:
        result = store.edit_item(meeting_id, item_id, body.changes, request.state.user["username"])
        reminders.run_once()
        return result
    except KeyError as exc:
        raise HTTPException(404, "Элемент не найден") from exc
    except ValueError as exc:
        raise _error(exc) from exc


@app.post("/api/meetings/{meeting_id}/items", status_code=201)
def add_item(meeting_id: str, body: ItemCreate, request: Request) -> dict:
    _meeting(meeting_id)
    if body.due_date:
        _validated_date(body.due_date)
    data = body.model_dump(exclude={"actor"})
    _validate_reminder(data)
    data["status"] = "draft"
    try:
        return store.add_item(meeting_id, data, request.state.user["username"])
    except ValueError as exc:
        raise _error(exc) from exc


@app.patch("/api/meetings/{meeting_id}/summary")
def edit_summary(meeting_id: str, body: SummaryEdit, request: Request) -> dict:
    entry = _meeting(meeting_id)
    if entry["state"] != "review":
        raise HTTPException(409, "Саммари меняется только до утверждения")
    store.audit(meeting_id, request.state.user["username"], "summary_edited", {"before": entry["summary"], "after": body.summary})
    return store.update_meeting(meeting_id, summary=body.summary.strip(), summary_topics=[])


@app.patch("/api/meetings/{meeting_id}/speakers/{speaker_id}")
def edit_speaker(meeting_id: str, speaker_id: str, body: SpeakerEdit, request: Request) -> dict:
    _meeting(meeting_id)
    try:
        return store.edit_speaker(meeting_id, speaker_id, body.name, request.state.user["username"])
    except KeyError as exc:
        raise HTTPException(404, "Говорящий не найден") from exc
    except ValueError as exc:
        raise _error(exc) from exc


@app.post("/api/meetings/{meeting_id}/approve")
def approve(meeting_id: str, body: Approval, request: Request) -> dict:
    _meeting(meeting_id)
    if not body.confirmed:
        raise HTTPException(422, "Требуется подтверждение проверки")
    try:
        return service.approve(meeting_id, request.state.user["username"])
    except ValueError as exc:
        raise _error(exc) from exc


@app.get("/api/meetings/{meeting_id}/export")
def export(meeting_id: str, format: Literal["pdf", "docx"], include_transcript: bool = True) -> StreamingResponse:
    entry = _meeting(meeting_id)
    if entry["state"] not in {"review", "approved", "error"} or not entry["summary"].strip() or not entry["segments"]:
        raise HTTPException(409, "Для скачивания нужны готовые саммари и транскрипт")
    payload = exports.pdf(entry, include_transcript=include_transcript) if format == "pdf" else exports.docx(entry, include_transcript=include_transcript)
    mime = "application/pdf" if format == "pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    prefix = "protocol-approved" if entry["state"] == "approved" else "draft-protocol"
    return StreamingResponse(BytesIO(payload), media_type=mime,
                             headers={"Content-Disposition": f'attachment; filename="{prefix}-{meeting_id}.{format}"'})


@app.get("/api/meetings/{meeting_id}/media")
def media(meeting_id: str) -> FileResponse:
    entry = _meeting(meeting_id)
    path = Path(entry["media_path"])
    if not path.is_file():
        raise HTTPException(404, "Исходная запись не найдена")
    return FileResponse(path)
