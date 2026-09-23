#!/bin/zsh
cd "${0:A:h}" || exit 1
if [[ ! -x .venv/bin/python ]]; then
  print -u2 "Python environment is missing. Create .venv and install requirements-client.txt."
  exit 1
fi
.venv/bin/python -m meeting_bot.teams_client --join-only
