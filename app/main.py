"""Local-only API and the secretary's review interface."""

from __future__ import annotations

import os
import importlib.util
import re
import shutil
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import exports, service, store
from .config import MODEL_DIR
from .model_files import ASR_BYTES, SPEAKER_BYTES, available


MAX_MEDIA_BYTES = int(os.getenv("PROTOCOL_MAX_MEDIA_BYTES", str(500 * 1024 * 1024)))
SUFFIXES = {".mp3", ".wav", ".m4a", ".mp4", ".webm", ".ogg"}
STATIC = Path(__file__).with_name("static")
app = FastAPI(title="AI Протоколист", version="0.1.0", docs_url="/api/docs", redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _meeting(meeting_id: str) -> dict:
    try:
        return store.get_meeting(meeting_id)
    except KeyError as exc:
        raise HTTPException(404, "Совещание не найдено") from exc


def _validated_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
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
    return {"status": "ok", "offline_runtime": True, "ffmpeg": shutil.which("ffmpeg") is not None,
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


@app.post("/api/meetings/live", status_code=201)
def start_recording(body: StartRecording) -> dict:
    if not body.consent_confirmed:
        raise HTTPException(422, "Подтвердите уведомление и согласие участников")
    if body.source == "platform":
        if not body.provider:
            raise HTTPException(422, "Выберите платформу")
        _validate_meeting_url(body.provider, body.meeting_url)
    elif body.provider or body.meeting_url:
        raise HTTPException(422, "Для записи в переговорной платформа не нужна")
    return store.create_meeting(title=body.title, meeting_date=_validated_date(body.meeting_date),
                                source=body.source, provider=body.provider,
                                participants=_participants(body.participants), suffix=".webm")


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
def edit_item(meeting_id: str, item_id: str, body: ItemEdit) -> dict:
    _meeting(meeting_id)
    if "status" in body.changes and body.changes["status"] not in {"draft", "in_progress", "done", "cancelled"}:
        raise HTTPException(422, "Недопустимый статус")
    try:
        return store.edit_item(meeting_id, item_id, body.changes, body.actor)
    except KeyError as exc:
        raise HTTPException(404, "Элемент не найден") from exc
    except ValueError as exc:
        raise _error(exc) from exc


@app.post("/api/meetings/{meeting_id}/items", status_code=201)
def add_item(meeting_id: str, body: ItemCreate) -> dict:
    _meeting(meeting_id)
    if body.due_date:
        _validated_date(body.due_date)
    data = body.model_dump(exclude={"actor"})
    data["status"] = "draft"
    try:
        return store.add_item(meeting_id, data, body.actor)
    except ValueError as exc:
        raise _error(exc) from exc


@app.patch("/api/meetings/{meeting_id}/summary")
def edit_summary(meeting_id: str, body: SummaryEdit) -> dict:
    entry = _meeting(meeting_id)
    if entry["state"] != "review":
        raise HTTPException(409, "Саммари меняется только до утверждения")
    store.audit(meeting_id, body.actor, "summary_edited", {"before": entry["summary"], "after": body.summary})
    return store.update_meeting(meeting_id, summary=body.summary.strip(), summary_topics=[])


@app.patch("/api/meetings/{meeting_id}/speakers/{speaker_id}")
def edit_speaker(meeting_id: str, speaker_id: str, body: SpeakerEdit) -> dict:
    _meeting(meeting_id)
    try:
        return store.edit_speaker(meeting_id, speaker_id, body.name, body.actor)
    except KeyError as exc:
        raise HTTPException(404, "Говорящий не найден") from exc
    except ValueError as exc:
        raise _error(exc) from exc


@app.post("/api/meetings/{meeting_id}/approve")
def approve(meeting_id: str, body: Approval) -> dict:
    _meeting(meeting_id)
    if not body.confirmed:
        raise HTTPException(422, "Требуется подтверждение проверки")
    try:
        return service.approve(meeting_id, body.actor)
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
