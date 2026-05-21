# services/svc-face/app/Dockerfile
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    LOG_LEVEL=info \
    PYTHONPATH=/app

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libheif-dev \
  && rm -rf /var/lib/apt/lists/*

COPY services/svc-face/app/app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY services/svc-face/app/app /app/app
COPY services/shared/python/desifaces_shared /app/desifaces_shared

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/api/health" || exit 1

CMD ["bash", "-lc", "uvicorn app.main:app --host ${HOST} --port ${PORT} --log-level ${LOG_LEVEL}"]