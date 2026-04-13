# /docker/backend.Dockerfile
# Build context is the repo root (see compose/docker-compose.yml context: ..)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

# ---- System deps ----
RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential libpq-dev gcc && \
    rm -rf /var/lib/apt/lists/*

# ---- Python deps (cacheable) ----
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# ---- App code ----
COPY backend/ ./
COPY types/ /types/
COPY build_info.json /app/build_info.json
COPY .scripts/stamp_build_time.py /app/scripts/stamp_build_time.py

RUN python /app/scripts/stamp_build_time.py /app/build_info.json

ARG APP_VERSION
LABEL org.opencontainers.image.version="${APP_VERSION}"

EXPOSE 8081
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8081", "--log-config", "/app/core/uvicorn_log_config.json"]
