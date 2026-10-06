# The WhatsApp bot, as a small container image. Built on the server by docker compose.
# Works on x86 (e.g. Google Cloud e2-micro) and ARM servers: the Python image supports both.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Run as a normal user, not root. uid 10001 must own ./data on the server (see docs/DEPLOY.md).
RUN useradd --create-home --uid 10001 bot

# Only the runtime packages (no pytest). Installed first so code changes rebuild faster.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY config ./config
COPY prompts ./prompts
COPY scripts ./scripts
RUN mkdir -p /app/data /app/secrets && chown bot:bot /app/data

USER bot
EXPOSE 8000

# Docker marks the container "unhealthy" if /health stops answering.
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os, sys, urllib.request; port = os.environ.get('PORT', '8000'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=4).status == 200 else 1)"

# Listens on $PORT if the host sets it (Railway does), otherwise 8000 (docker compose).
# Exactly ONE worker: chat locks, alert memory and Telegram polling live in one process.
# No access log: Meta's webhook check puts the verify token in the URL.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --no-access-log"]
