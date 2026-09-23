"""Single local processing pipeline for files, room microphones, and meeting tabs."""

from __future__ import annotations

import logging
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import inference, store


LOGGER = logging.getLogger(__name__)
_WORKER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="meeting-worker")


def process(meeting_id: str) -> None:
    meeting = store.get_meeting(meeting_id)
    source = Path(meeting["media_path"])
    wav = source.parent / "audio-16k.wav"
    try:
        store.update_meeting(meeting_id, state="processing", stage="Подготовка аудио", error=None)
        inference.normalize_audio(source, wav)
        store.update_meeting(meeting_id, stage="Распознавание речи")
        words = inference.transcribe(wav, on_progress=lambda percent: store.update_meeting(
            meeting_id, stage=f"Распознавание речи · {percent}% аудио"))
        store.update_meeting(meeting_id, stage="Различение говорящих")
        spans = inference.diarize_audio(wav, len(meeting["participants"]) or None)
        segments = inference.align_words(words, spans)
        speakers = inference.infer_speaker_names(segments)
        store.update_meeting(meeting_id, stage="Анализ решений и поручений", segments=segments,
                             speakers=speakers)
        result = inference.extract(segments, meeting["participants"], meeting["meeting_date"])
        store.update_meeting(meeting_id, stage="Формирование тематического саммари", items=result["items"])
        summary = inference.summarize(segments)
        store.update_meeting(meeting_id, state="review", stage="Ожидает проверки секретарём",
                             summary=summary, items=result["items"])
        store.audit(meeting_id, "system", "processing_completed",
                    {"turns": len(segments), "items": len(result["items"])})
    except Exception as exc:
        LOGGER.exception("Meeting %s failed", meeting_id)
        store.update_meeting(meeting_id, state="error", stage="Ошибка обработки", error=str(exc))
        store.audit(meeting_id, "system", "processing_failed", {"message": str(exc)})


def enqueue(meeting_id: str) -> dict[str, Any]:
    meeting = store.get_meeting(meeting_id)
    if meeting["state"] not in {"queued", "recording", "error"}:
        raise ValueError("Это совещание уже обрабатывается или утверждено")
    resume_analysis = meeting["state"] == "error" and bool(meeting["segments"])
    if not resume_analysis:
        media = Path(meeting["media_path"])
        if not media.exists() or media.stat().st_size < 1000:
            raise ValueError("Запись пуста или ещё не сохранена")
    store.update_meeting(meeting_id, state="queued",
                         stage="В очереди на повторный анализ" if resume_analysis else "В очереди", error=None)
    _WORKER.submit(_analyze_revision if resume_analysis else process, meeting_id)
    return store.get_meeting(meeting_id)


def reanalyze(meeting_id: str) -> dict[str, Any]:
    original = store.get_meeting(meeting_id)
    if not original["segments"]:
        raise ValueError("Сначала необходимо распознать речь")
    source = Path(original["media_path"])
    revision = store.create_meeting(title=original["title"] + " · новая редакция",
        meeting_date=original["meeting_date"], source="upload", provider=original["provider"],
        participants=original["participants"], suffix=source.suffix)
    shutil.copyfile(source, revision["media_path"])
    store.update_meeting(revision["id"], segments=original["segments"], gaps=original["gaps"],
        speakers=inference.infer_speaker_names(original["segments"]), state="queued", stage="Обновление AI-анализа")
    store.audit(revision["id"], "system", "revision_created", {"source_meeting_id": meeting_id})
    _WORKER.submit(_analyze_revision, revision["id"])
    return store.get_meeting(revision["id"])


def _analyze_revision(meeting_id: str) -> None:
    meeting = store.get_meeting(meeting_id)
    try:
        store.update_meeting(meeting_id, state="processing", stage="Уточнение поручений"
                             if meeting["summary"] else "Формирование тематического саммари")
        if not meeting["summary"]:
            summary = inference.summarize(meeting["segments"])
            store.update_meeting(meeting_id, summary=summary, stage="Уточнение поручений")
        result = inference.extract(meeting["segments"], meeting["participants"], meeting["meeting_date"])
        store.update_meeting(meeting_id, items=result["items"], state="review", stage="Ожидает проверки секретарём")
    except Exception as exc:
        LOGGER.exception("Reanalysis failed for %s", meeting_id)
        store.update_meeting(meeting_id, state="error", stage="Ошибка анализа", error=str(exc))


def approve(meeting_id: str, actor: str) -> dict[str, Any]:
    meeting = store.get_meeting(meeting_id)
    if meeting["state"] != "review":
        raise ValueError("Протокол ещё не готов к проверке")
    if not meeting["segments"]:
        raise ValueError("Нельзя утвердить протокол без транскрипта")
    speaker_ids = {segment.get("speaker_id") for segment in meeting["segments"]}
    names = meeting["speakers"]
    if (not speaker_ids or not all(speaker_ids) or speaker_ids - names.keys()
            or any(not isinstance(name, str) or not name.strip() or name.strip().endswith("(проверьте)")
                   for name in names.values())):
        raise ValueError("Перед утверждением подтвердите имена всех говорящих или укажите «Не установлен»")
    if not meeting["summary"] or len(meeting["summary"]) < 100:
        raise ValueError("Саммари пустое или слишком краткое; проверьте его перед утверждением")
    store.audit(meeting_id, actor, "approved", {"items": len(meeting["items"])})
    return store.update_meeting(meeting_id, state="approved", stage="Утверждён",
                                approved_at=store.now())


def dashboard() -> dict[str, Any]:
    from datetime import date, timedelta

    today = date.today()
    actions = []
    for meeting in store.list_meetings():
        if meeting["state"] != "approved":
            continue
        for item in meeting["items"]:
            if item["kind"] != "action" or item["status"] == "cancelled":
                continue
            current = dict(item)
            current["meeting_id"] = meeting["id"]
            current["meeting_title"] = meeting["title"]
            if current["status"] == "done":
                current["deadline_state"] = "done"
            elif current.get("due_date"):
                due = date.fromisoformat(current["due_date"])
                current["deadline_state"] = "overdue" if due < today else (
                    "soon" if due <= today + timedelta(days=3) else "on_track")
            else:
                current["deadline_state"] = "no_date"
            actions.append(current)
    counts = {key: sum(item["deadline_state"] == key for item in actions)
              for key in ("overdue", "soon", "on_track", "no_date", "done")}
    return {"counts": counts, "actions": actions}
