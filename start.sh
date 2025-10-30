#!/usr/bin/env bash
set -e
python -m pip install -r requirements.txt
# экспорт переменных окружения из .env (если есть)
if [ -f .env ]; then
  export $(grep -v '^#' .env | xargs -d '\n' -I {} echo {})
fi
python bot.py
