FROM aiogram/telegram-bot-api:latest AS tg-api

FROM python:3.11-alpine

RUN apk add --no-cache \
    libstdc++ \
    openssl \
    zlib \
    ca-certificates

COPY --from=tg-api /usr/local/bin/telegram-bot-api /usr/local/bin/telegram-bot-api

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

RUN mkdir -p /tmp/tg-bot-api /tmp/tg-bot-api-temp /tmp/zip_bot_workspace

CMD ["python3", "bot.py"]
