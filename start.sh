#!/bin/bash
set -e

echo "Starting Local Telegram Bot API Server on port 8081..."
telegram-bot-api \
  --local \
  --api-id="${API_ID}" \
  --api-hash="${API_HASH}" \
  --http-port=8081 \
  --dir=/tmp/tg-bot-api \
  --temp-dir=/tmp/tg-bot-api-temp \
  --verbosity=1 &

# Local Bot API ko'tarilishi uchun 3 soniya kutamiz
sleep 3

echo "Starting Python Aiogram ZIP Bot..."
exec python3 bot.py