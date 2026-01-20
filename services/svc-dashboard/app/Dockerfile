FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
  && rm -rf /var/lib/apt/lists/*

COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy application code
COPY app /app/app

# Ensure "app" package resolves correctly (since code lives in /app/app)
ENV PYTHONPATH="/app/app" \
    HOST="0.0.0.0" \
    PORT="8000" \
    LOG_LEVEL="info"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/api/health" || exit 1

# Default: API server
CMD ["bash", "-lc", "uvicorn app.main:app --host ${HOST} --port ${PORT} --log-level ${LOG_LEVEL}"]