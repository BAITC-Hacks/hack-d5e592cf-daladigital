"""Offline speech, speaker, and decision extraction adapters."""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx


KINDS = {"action", "decision", "initiative", "question", "risk"}
MODEL_DIR = Path(os.getenv("PROTOCOL_MODEL_DIR", ".data/models")).resolve()


def normalize_audio(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
               "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(target)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=900, check=False)
    if result.returncode != 0 or not target.exists() or target.stat().st_size < 1000:
        raise RuntimeError(f"Не удалось прочитать аудио: {result.stderr[-400:]}")


def transcribe(wav: Path) -> list[dict[str, Any]]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("Установите requirements-ai.txt для локального распознавания речи") from exc
    model_path = MODEL_DIR / "whisper-large-v3-turbo"
    if not model_path.exists():
        raise RuntimeError("Модель Whisper не загружена. Запустите python -m app.prepare_models")
    model = WhisperModel(str(model_path), device="cpu", compute_type="int8", cpu_threads=6,
                         local_files_only=True)
    segments, _ = model.transcribe(str(wav), beam_size=5, word_timestamps=True,
                                   vad_filter=True, multilingual=True,
                                   condition_on_previous_text=False, task="transcribe")
    output: list[dict[str, Any]] = []
    for segment in segments:
        if segment.words:
            for word in segment.words:
                text = word.word.strip()
                if text:
                    output.append({"start": round(word.start, 2), "end": round(word.end, 2),
                                   "text": text, "confidence": round(word.probability, 3)})
        elif segment.text.strip():
            output.append({"start": round(segment.start, 2), "end": round(segment.end, 2),
                           "text": segment.text.strip(), "confidence": None})
    if not output:
        raise RuntimeError("Речь не распознана. Проверьте, что запись содержит слышимый голос")
    return output


def diarize_audio(wav: Path, expected_speakers: int | None = None) -> list[dict[str, Any]]:
    try:
        from diarize import diarize
    except ImportError as exc:
        raise RuntimeError("Установите requirements-ai.txt для диаризации") from exc
    # Explicitly resolve the local embedding model. The package's default resolver
    # downloads into the user's home directory, which is unsuitable for an offline run.
    speaker_model = MODEL_DIR / "wespeaker" / "model.onnx"
    if not speaker_model.is_file() or speaker_model.stat().st_size != 26_530_309:
        raise RuntimeError("Модель диаризации не загружена. Запустите python -m app.prepare_models")
    from wespeakerruntime.hub import Hub
    Hub.get_model_by_lang = staticmethod(lambda _lang: str(speaker_model))
    # A participant list is not a reliable speaker count (some may not speak).
    result = diarize(str(wav))
    spans = [{"start": round(float(segment.start), 2), "end": round(float(segment.end), 2),
              "speaker_id": str(segment.speaker)} for segment in result.segments]
    if not spans:
        raise RuntimeError("Диаризация не обнаружила голосовых сегментов")
    return spans


def _speaker_for(word: dict[str, Any], spans: list[dict[str, Any]]) -> str:
    start, end = word["start"], word["end"]
    best = max(spans, key=lambda span: (max(0.0, min(end, span["end"]) - max(start, span["start"])),
                                        -abs((start + end) / 2 - (span["start"] + span["end"]) / 2)))
    return best["speaker_id"]


def align_words(words: list[dict[str, Any]], spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Assign each ASR word to one speaker and merge adjacent words into readable turns."""
    turns: list[dict[str, Any]] = []
    for word in words:
        speaker = _speaker_for(word, spans)
        if turns and turns[-1]["speaker_id"] == speaker and word["start"] - turns[-1]["end"] < 1.7 \
                and word["end"] - turns[-1]["start"] < 35:
            turn = turns[-1]
            turn["text"] += ("" if word["text"].startswith((".", ",", "!", "?", ":", ";")) else " ") + word["text"]
            turn["end"] = word["end"]
            turn["confidence_values"].append(word["confidence"])
        else:
            turns.append({"id": str(len(turns) + 1), "start": word["start"], "end": word["end"],
                          "speaker_id": speaker, "text": word["text"],
                          "confidence_values": [word["confidence"]]})
    for turn in turns:
        values = [x for x in turn.pop("confidence_values") if x is not None]
        turn["confidence"] = round(sum(values) / len(values), 3) if values else None
    return turns


_PERSON = re.compile(r"\b([А-ЯӘІҢҒҮҰҚӨҺЁ][а-яәіңғүұқөһё]+)\s+([А-ЯӘІҢҒҮҰҚӨҺЁ][а-яәіңғүұқөһё]+(?:ович|евич|овна|евна|қызы|ұлы))\b")
_ADDRESS = re.compile(r"вам слово|что у вас|что предлагаете|вы же|по остальным площадкам|можно добавить|последний раз|как вы|ваш[а-я]* мнени", re.I)


def infer_speaker_names(segments: list[dict[str, Any]]) -> dict[str, str]:
    """Suggest names from direct address + the next voice turn; never claim identity is verified."""
    votes: dict[str, Counter[str]] = defaultdict(Counter)
    for previous, following in zip(segments, segments[1:]):
        if previous["speaker_id"] == following["speaker_id"] or following["start"] - previous["end"] > 5:
            continue
        if not _ADDRESS.search(previous["text"]):
            continue
        matches = list(_PERSON.finditer(previous["text"]))
        if not matches:
            continue
        # A speaker may name several people; a name at the start is typically
        # a direct address, otherwise use the last address before the question.
        match = matches[0] if matches[0].start() < 35 else matches[-1]
        votes[following["speaker_id"]][match.group()] += 1
    speakers = {segment["speaker_id"] for segment in segments}
    return {speaker: (votes[speaker].most_common(1)[0][0] if votes[speaker] else "Имя не установлено")
            + " (проверьте)" for speaker in speakers}


_SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "properties": {"topics": {"type": "array", "minItems": 1,
        "items": {"type": "object", "additionalProperties": False,
            "properties": {"title": {"type": "string"}, "text": {"type": "string"},
                           "source_segment_ids": {"type": "array", "items": {"type": "string"}}},
            "required": ["title", "text", "source_segment_ids"]}}},
    "required": ["topics"],
}


def _local_json(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
    model = os.getenv("PROTOCOL_OLLAMA_MODEL", "qwen3:4b")
    endpoint = os.getenv("PROTOCOL_OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
    payload = {"model": model, "stream": False, "format": schema, "think": False,
               "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 6000},
               "messages": [{"role": "user", "content": prompt}]}
    try:
        with httpx.Client(timeout=300, trust_env=False) as client:
            response = client.post(endpoint, json=payload)
            response.raise_for_status()
            return json.loads(response.json()["message"]["content"])
    except (httpx.HTTPError, KeyError, json.JSONDecodeError) as exc:
        raise RuntimeError("Локальная модель анализа недоступна. Проверьте Ollama и qwen3:4b") from exc


def summarize(segments: list[dict[str, Any]]) -> str:
    transcript = "\n".join(f"[{s['id']}] {s['text']}" for s in segments)
    prompt = (
        "Составь содержательное управленческое саммари совещания на русском языке. "
        "Верни 2–4 тематических раздела по повестке (если тема одна — один раздел). "
        "Для каждой темы напиши 2–4 связных предложения, не менее 100 символов: "
        "текущее положение с точными цифрами, причина проблемы, последствия или риск. "
        "Нужен стиль хорошего протокола: конкретные показатели и причинно-следственные связи, без общих фраз. "
        "Цифры и факты бери ТОЛЬКО из транскрипта ниже. "
        "Не копируй заголовок задачи, не пиши 'краткое фактическое саммари'. "
        "Не пересказывай каждое поручение — для этого есть отдельный реестр. "
        "Если в разных репликах расходятся названия/цифры, не выбирай наугад: отметь необходимость проверки. "
        "Укажи номера реплик, подтверждающих каждый раздел.\nТранскрипт:\n" + transcript
    )
    raw = _local_json(prompt, _SUMMARY_SCHEMA)
    valid_ids = {segment["id"] for segment in segments}
    source_text = " ".join(segment["text"] for segment in segments)
    source_numbers = set(re.findall(r"\d+(?:[.,]\d+)?", source_text))
    blocks = []
    for topic in raw.get("topics", []):
        title = str(topic.get("title", "")).strip()
        body = str(topic.get("text", "")).strip()
        evidence = [str(x) for x in topic.get("source_segment_ids", []) if str(x) in valid_ids]
        numbers = set(re.findall(r"\d+(?:[.,]\d+)?", body))
        if not title or len(body) < 100 or not evidence or numbers - source_numbers:
            continue
        blocks.append(f"{title}\n{body}")
    if not blocks:
        raise RuntimeError("Саммари не прошло проверку качества; требуется повтор анализа или ручная правка")
    return "\n\n".join(blocks)


_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "items": {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "properties": {
                "kind": {"type": "string", "enum": sorted(KINDS)},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "owner": {"type": ["string", "null"]},
                "due_text": {"type": ["string", "null"]},
                "source_segment_ids": {"type": "array", "items": {"type": "string"}},
                "status": {"type": "string", "enum": ["active", "cancelled"]},
                "review_note": {"type": "string"},
            },
            "required": ["kind", "title", "description", "owner", "due_text", "source_segment_ids",
                         "status", "review_note"]}},
    }, "required": ["items"],
}


def _prompt(segments: list[dict[str, Any]], participants: list[str], meeting_date: str) -> str:
    lines = [f"[{s['id']}] {s['speaker_id']} {s['start']:.1f}-{s['end']:.1f}: {s['text']}" for s in segments]
    return (
        "Ты локальный ассистент протоколиста АО Самрук-Қазына. Анализируй русскую, казахскую и смешанную речь. "
        "Выдай итоговый список уникальных элементов. Саммари создаётся отдельным этапом. "
        "Типы: action = конкретное поручение; decision = принятое решение; initiative = стратегическое направление "
        "без конкретного поручения; question = открытый вопрос; risk = обозначенный риск. "
        "Фраза 'нужно развить отрасль' — initiative, не action. "
        "Не придумывай фамилии, сроки и факты. Различай говорящего и назначенного исполнителя. "
        "Учитывай согласие исполнителя, изменение срока, отмену и итоговое повторение: возвращай только актуальную версию, "
        "а отменённому ставь status=cancelled. Если ответственный или срок не названы — null и пояснение в review_note. "
        "Для каждого элемента укажи ID исходных реплик, где он подтверждается. "
        f"Дата совещания: {meeting_date}. Участники: {', '.join(participants) or 'не указаны'}.\n"
        "Транскрипт:\n" + "\n".join(lines)
    )


def _normalize_due(raw: str | None, meeting_date: str) -> str | None:
    if not raw:
        return None
    import re
    start = date.fromisoformat(meeting_date)
    lowered = raw.casefold()
    months = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
              "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12}
    match = re.search(r"\b(\d{1,2})\s+([а-яё]+)(?:\s+(20\d{2}))?", lowered)
    if match:
        month = next((number for stem, number in months.items() if match.group(2).startswith(stem)), None)
        if month:
            try:
                candidate = date(int(match.group(3)) if match.group(3) else start.year, month, int(match.group(1)))
                if not match.group(3) and candidate < start:
                    candidate = candidate.replace(year=start.year + 1)
                return candidate.isoformat()
            except ValueError:
                return None
    if "завтра" in lowered or "ертең" in lowered:
        return (start + timedelta(days=1)).isoformat()
    if "через две недели" in lowered or "за две недели" in lowered:
        return (start + timedelta(days=14)).isoformat()
    if "через неделю" in lowered or "за неделю" in lowered:
        return (start + timedelta(days=7)).isoformat()
    return None


def extract(segments: list[dict[str, Any]], participants: list[str], meeting_date: str) -> dict[str, Any]:
    raw = _local_json(_prompt(segments, participants, meeting_date), _ITEM_SCHEMA)
    valid_ids = {segment["id"] for segment in segments}
    items = []
    for candidate in raw.get("items", []):
        if candidate.get("kind") not in KINDS or not candidate.get("title", "").strip():
            continue
        source_ids = [str(x) for x in candidate.get("source_segment_ids", []) if str(x) in valid_ids]
        if not source_ids:
            continue
        owner = (candidate.get("owner") or "").strip() or None
        due_text = (candidate.get("due_text") or "").strip() or None
        kind = candidate["kind"]
        evidence_text = " ".join(segment["text"] for segment in segments if segment["id"] in source_ids).casefold()
        review_note = candidate.get("review_note", "").strip()
        grounded_due = bool(due_text and due_text.casefold() in evidence_text)
        if due_text and not grounded_due:
            review_note = (review_note + " Срок не найден дословно в указанных репликах; проверьте по аудио.").strip()
        items.append({"id": uuid.uuid4().hex[:10], "kind": kind, "title": candidate["title"].strip(),
                      "description": candidate.get("description", "").strip(), "owner": owner,
                      "due_text": due_text, "due_date": _normalize_due(due_text, meeting_date) if grounded_due else None,
                      "source_segment_ids": source_ids,
                      "status": "cancelled" if candidate.get("status") == "cancelled" else "draft",
                      "review_note": review_note})
    return {"items": items}
