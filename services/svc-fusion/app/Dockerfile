FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8002 \
    LOG_LEVEL=info \
    PYTHONPATH=/app:/repo:/repo/shared

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    curl \
    ca-certificates \
  && rm -rf /var/lib/apt/lists/*

# svc-fusion requirements live under the nested app/ folder
COPY services/svc-fusion/app/app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# copy full repo so shared imports like desifaces_shared are available
COPY . /repo

# keep runtime import layout expected by uvicorn app.main:app
COPY services/svc-fusion/app/app /app/app
COPY services/shared/python/desifaces_shared /app/desifaces_shared

EXPOSE 8002

HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/api/health" || exit 1

CMD ["bash", "-lc", "uvicorn app.main:app --host ${HOST} --port ${PORT} --log-level ${LOG_LEVEL}"]
