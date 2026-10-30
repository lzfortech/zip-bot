# 1-bosqich: Tayyor Local Bot API binar faylini olish (Alpine asosida)
FROM aiogram/telegram-bot-api:latest AS tg-api

# 2-bosqich: Python ni ham Alpine asosida olamiz (100% moslik uchun)
FROM python:3.11-alpine

# Local Bot API va bash ishlashi uchun kerakli Alpine paketlari
RUN apk add --no-cache \
    bash \
    libstdc++ \
    openssl \
    zlib \
    ca-certificates

# Local Bot API binar faylini ko'chirib o'tkazamiz
COPY --from=tg-api /usr/local/bin/telegram-bot-api /usr/local/bin/telegram-bot-api

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY start.sh .

# Windows CRLF (\r) belgilarini tozalash va ruxsat berish
RUN sed -i 's/\r$//' /app/start.sh && \
    chmod +x /app/start.sh && \
    mkdir -p /tmp/tg-bot-api /tmp/tg-bot-api-temp /tmp/zip_bot_workspace

CMD ["/bin/bash", "/app/start.sh"]
