# Build from this directory (tg-app-cloud) as the root context.
# docker compose up --build

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    TG_UPLOAD_DIR=/app/tg-upload \
    CRYPT_CONFIG=/data/crypt.conf

RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates unzip ffmpeg \
    && curl -fsSL https://rclone.org/install.sh | bash \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
COPY tg-upload/requirements.txt /app/tg-upload-requirements.txt
# tgcrypto needs a C compiler; remove build tools after install to keep image lean
RUN apt-get update && apt-get install -y --no-install-recommends \
      gcc \
      python3-dev \
    && pip install --no-cache-dir -r /app/requirements.txt \
    && pip install --no-cache-dir -r /app/tg-upload-requirements.txt \
    && python -c "import pyrogram; print('pyrogram', pyrogram.__version__)" \
    && apt-get purge -y --auto-remove gcc python3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY tg-upload /app/tg-upload
COPY app /app/app
COPY crypt.conf.example /app/crypt.conf.example
COPY entrypoint.sh /app/entrypoint.sh

RUN mkdir -p /data/uploads /data/downloads /data/jobs \
    && chmod +x /app/entrypoint.sh \
    && useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app /data

# Entrypoint starts as root to chown bind-mounted /data, then drops to appuser
USER root

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
