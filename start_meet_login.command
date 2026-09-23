#!/bin/zsh
cd "${0:A:h}" || exit 1
exec .venv/bin/python -u -m meeting_bot.meet_client --login "$@"
