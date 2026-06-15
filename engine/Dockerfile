FROM python:3.11-slim

# System deps kept minimal. ffmpeg/ffprobe intentionally omitted in v1
# (we use HEAD + manifest sniff; see plan.md §6). Add later for deep probes.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY engine ./engine
COPY migrations ./migrations

# data and logs are mounted as volumes; create dirs so the entrypoint can write
RUN mkdir -p /app/data /app/logs

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Lightweight built-in probe — confirms the engine is up.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import socket; s=socket.socket(); s.settimeout(2); s.connect(('127.0.0.1', 8000))" || exit 1

# Default: scheduler loop. Override with `python -m engine seed --m3u <url>`
# or `python -m engine check-once` for one-shot operations.
CMD ["python", "-m", "engine"]
