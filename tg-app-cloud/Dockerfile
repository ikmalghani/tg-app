# Build from repo root so tg-upload can be copied into the image.
# docker compose -f tg-app-cloud/docker-compose.yml up --build

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    TG_UPLOAD_DIR=/app/tg-upload \
    CRYPT_CONFIG=/app/crypt.conf

RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates unzip ffmpeg \
    && curl -fsSL https://rclone.org/install.sh | bash \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY tg-app-cloud/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY tg-upload /app/tg-upload
COPY tg-app-cloud/app /app/app
COPY tg-app-cloud/entrypoint.sh /app/entrypoint.sh

RUN mkdir -p /data/uploads /data/downloads /data/jobs \
    && chmod +x /app/entrypoint.sh \
    && useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app /data

USER appuser

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
