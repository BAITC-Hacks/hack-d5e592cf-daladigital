#!/usr/bin/env bash
set -euo pipefail

protocol_repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$protocol_repo_dir"

if [[ -n "${PROTOCOL_HOME:-}" ]]; then
  protocol_app_home="$PROTOCOL_HOME"
elif [[ "$(uname -s)" == "Darwin" ]]; then
  protocol_app_home="$HOME/Library/Application Support/AI-Protokolist"
else
  protocol_app_home="${XDG_DATA_HOME:-$HOME/.local/share}/ai-protokolist"
fi
export PROTOCOL_HOME="$protocol_app_home"
protocol_runtime="$protocol_app_home/runtime"

for protocol_command in ffmpeg ffprobe ollama; do
  if ! command -v "$protocol_command" >/dev/null 2>&1; then
    printf 'Не найден %s. Установите его и повторите bash scripts/setup.sh.\n' "$protocol_command" >&2
    exit 1
  fi
done

protocol_python="${PROTOCOL_PYTHON:-}"
if [[ -z "$protocol_python" ]]; then
  for protocol_candidate in python3.12 python3.11 python3; do
    if command -v "$protocol_candidate" >/dev/null 2>&1 && "$protocol_candidate" -c 'import sys; sys.exit(not ((3, 11) <= sys.version_info[:2] <= (3, 12)))' >/dev/null 2>&1; then
      protocol_python="$protocol_candidate"
      break
    fi
  done
fi
if [[ -z "$protocol_python" ]] || ! "$protocol_python" -c 'import sys; sys.exit(not ((3, 11) <= sys.version_info[:2] <= (3, 12)))' >/dev/null 2>&1; then
  printf 'Нужен Python 3.11 или 3.12. Укажите путь: PROTOCOL_PYTHON=/path/to/python3.12 bash scripts/setup.sh\n' >&2
  exit 1
fi

mkdir -p "$protocol_app_home"
if [[ ! -x "$protocol_runtime/bin/python" ]]; then
  "$protocol_python" -m venv "$protocol_runtime"
fi
printf 'Установка зависимостей: %s\n' "$protocol_runtime"
"$protocol_runtime/bin/python" -m pip install -r requirements-ai-lock.txt
"$protocol_runtime/bin/python" -m app.prepare_models
if ! ollama pull "${PROTOCOL_OLLAMA_MODEL:-qwen3:4b}"; then
  printf 'Не удалось подготовить модель анализа. Запустите приложение Ollama или ollama serve в другом терминале, затем повторите bash scripts/setup.sh.\n' >&2
  exit 1
fi
printf '\nГотово. Запуск: bash scripts/start.sh\n'
