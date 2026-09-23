#!/bin/zsh
cd "${0:A:h}" || exit 1
if [[ ! -x .venv/bin/python ]]; then
  print -u2 "Python environment is missing. Create .venv and install requirements-client.txt and requirements-openai.txt."
  exit 1
fi
if [[ -z ${OPENAI_API_KEY:-} ]]; then
  read -rs "OPENAI_API_KEY?Введите OpenAI API key (ввод скрыт): "
  print
  export OPENAI_API_KEY
fi
if [[ -z ${OPENAI_API_KEY:-} ]]; then
  print -u2 "OpenAI API key is required for transcription and summary."
  exit 1
fi
exec .venv/bin/python -u -m meeting_bot.meet_client "$@"
