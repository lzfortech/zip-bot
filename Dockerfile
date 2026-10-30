# 1-bosqich: Tayyor Local Bot API binar faylini olish
FROM aiogram/telegram-bot-api:latest AS tg-api

# 2-bosqich: Asosiy Python konteyneri
FROM python:3.11-slim

# Local Bot API ishlashi uchun kerakli tizim kutubxonalari (OpenSSL, zlib)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libssl3 \
    zlib1g \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Local Bot API binar faylini ko'chirib o'tkazamiz
COPY --from=tg-api /usr/local/bin/telegram-bot-api /usr/local/bin/telegram-bot-api

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY start.sh .

RUN chmod +x /app/start.sh && \
    mkdir -p /tmp/tg-bot-api /tmp/tg-bot-api-temp /tmp/zip_bot_workspace

CMD ["/app/start.sh"]