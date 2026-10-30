#!/bin/bash

echo "Starting Local Telegram Bot API Server on port 8081..."
/usr/local/bin/telegram-bot-api \
  --local \
  --api-id="${API_ID}" \
  --api-hash="${API_HASH}" \
  --http-port=8081 \
  --dir=/tmp/tg-bot-api \
  --temp-dir=/tmp/tg-bot-api-temp \
  --verbosity=1 &

# Local Bot API 8081-portni ochishini kutish (maksimum 15 soniya)
for i in {1..15}; do
  if nc -z 127.0.0.1 8081 2>/dev/null; then
    echo "Local Bot API is UP!"
    break
  fi
  echo "Waiting for Local Bot API... ($i)"
  sleep 1
done

echo "Starting Python Aiogram ZIP Bot..."
exec python3 bot.py
