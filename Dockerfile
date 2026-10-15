# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PITWALL_DATA_DIR=/app/data

WORKDIR /app

# Unprivileged runtime user. The UID/GID can be overridden in docker-compose (`user:`) to match
# the owner of ./data on the host.
RUN groupadd --gid 1000 pitwall \
 && useradd --uid 1000 --gid 1000 --no-create-home --home-dir /app --shell /usr/sbin/nologin pitwall

COPY pyproject.toml README.md ./
COPY pitwall ./pitwall
RUN pip install . && rm -rf /app/pitwall /app/build

# Fallback copy of the curated circuit data, used if ./data/circuits.yaml is missing.
COPY data/circuits.yaml /app/seed/circuits.yaml

RUN mkdir -p /app/data && chown pitwall:pitwall /app/data
VOLUME ["/app/data"]
USER pitwall

EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=8).status == 200 else 1)"]

# Applies database migrations, then starts the web app + scheduler.
CMD ["python", "-m", "pitwall", "serve", "--port", "8080"]
