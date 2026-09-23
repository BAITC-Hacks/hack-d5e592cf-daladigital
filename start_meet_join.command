#!/bin/zsh
cd "${0:A:h}" || exit 1
if [[ ! -x .venv/bin/python ]]; then
  print -u2 "Python environment is missing. Create .venv and install requirements-client.txt."
  exit 1
fi
exec .venv/bin/python -u -m meeting_bot.meet_client --join-only "$@"
