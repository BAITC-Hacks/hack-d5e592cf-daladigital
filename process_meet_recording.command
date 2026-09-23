#!/bin/zsh
cd "${0:A:h}" || exit 1
if [[ $# -eq 0 ]]; then
  read "meeting_session?Имя папки встречи в data/: "
  set -- --session "$meeting_session"
fi
exec .venv/bin/python -u -m meeting_bot.openai_process "$@"
