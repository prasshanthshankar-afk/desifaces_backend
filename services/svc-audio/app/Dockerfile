FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    HOST=0.0.0.0 \
    PORT=8004 \
    LOG_LEVEL=info

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
  && rm -rf /var/lib/apt/lists/*

COPY services/svc-audio/app/app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY services/shared/python/desifaces_shared /app/desifaces_shared
COPY services/shared/llm /app/desifaces_shared/llm
COPY services/shared/df_contracts /app/df_contracts
COPY services/svc-audio/app/app /app/app

EXPOSE 8004

HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/api/health" || exit 1

CMD ["sh", "-lc", "uvicorn app.main:app --host ${HOST} --port ${PORT} --log-level ${LOG_LEVEL}"]
