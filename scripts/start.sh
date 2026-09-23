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
protocol_python="$protocol_app_home/runtime/bin/python"

if [[ ! -x "$protocol_python" ]]; then
  printf 'Среда приложения ещё не установлена: %s\nВыполните bash scripts/setup.sh, затем повторите запуск.\n' "$protocol_python" >&2
  exit 1
fi
if ! "$protocol_python" -c 'import uvicorn' >/dev/null 2>&1; then
  printf 'Среда приложения неполная. Восстановите её командой bash scripts/setup.sh.\n' >&2
  exit 1
fi
printf 'AI Протоколист: http://127.0.0.1:%s\nХранилище: %s\n' "${PROTOCOL_PORT:-8000}" "$protocol_app_home"
exec "$protocol_python" -m uvicorn app.main:app --host 127.0.0.1 --port "${PROTOCOL_PORT:-8000}"
