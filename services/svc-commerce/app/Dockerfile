FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# System deps (asyncpg/libpq + curl for healthchecks)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    libpq-dev \
  && rm -rf /var/lib/apt/lists/*

# Install Python deps first (better layer caching)
COPY app/requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip install -r /app/requirements.txt

# Copy service code
COPY app /app/app

# Default port (compose overrides via $PORT)
EXPOSE 8008

# Compose supplies the command:
# - API:    uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
# - Worker: python -m app.workers.commerce_worker
CMD ["bash", "-lc", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8008}"]