"""Pinned model assets and quick, offline integrity checks."""

import hashlib
from functools import lru_cache
from pathlib import Path

ASR_REPO = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
ASR_REVISION = "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf"
ASR_BYTES = 1_617_884_929
ASR_SHA256 = "e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da"
SPEAKER_BYTES = 26_530_309
SPEAKER_URL = "https://wespeaker-1256283475.cos.ap-shanghai.myqcloud.com/models/voxceleb/voxceleb_resnet34_LM.onnx"


def available(path: Path, expected_size: int | None = None) -> bool:
    try:
        info = path.stat()
        return info.st_size > 0 and (expected_size is None or info.st_size == expected_size) \
            and getattr(info, "st_blocks", 1) > 0
    except OSError:
        return False


@lru_cache(maxsize=8)
def _digest(path: str, mtime: int, size: int) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_asr(directory: Path) -> None:
    model = directory / "model.bin"
    if not available(model, ASR_BYTES):
        raise RuntimeError("Модель распознавания отсутствует, неполная или выгружена macOS. "
                           "Восстановите модели командой bash scripts/setup.sh; запись совещания сохранена.")
    info = model.stat()
    if _digest(str(model), info.st_mtime_ns, info.st_size) != ASR_SHA256:
        raise RuntimeError("Контрольная сумма модели распознавания не совпадает. "
                           "Запустите bash scripts/setup.sh для восстановления модели.")
    if any(not available(directory / name) for name in ("config.json", "tokenizer.json", "preprocessor_config.json")):
        raise RuntimeError("Не хватает файлов настроек модели. Запустите bash scripts/setup.sh")
