# syntax=docker/dockerfile:1

FROM python:3.14.7-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install .

FROM python:3.14.7-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    APP_HOME=/app \
    TEMP_STORAGE_PATH=/var/lib/fetchnow/tmp

# ffmpeg/ffprobe: Debian bookworm official apt package (not compiled here).
# Version token is the distro package; worker logs never dump `--version` text.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 fetchnow \
    && useradd --system --uid 10001 --gid fetchnow --home-dir /app --shell /usr/sbin/nologin fetchnow \
    && mkdir -p /app \
        /var/lib/fetchnow/tmp/downloads \
        /var/lib/fetchnow/tmp/media_inspection \
    && chown -R fetchnow:fetchnow /app /var/lib/fetchnow \
    && chmod 0700 /var/lib/fetchnow/tmp \
        /var/lib/fetchnow/tmp/downloads \
        /var/lib/fetchnow/tmp/media_inspection

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=fetchnow:fetchnow pyproject.toml alembic.ini ./
COPY --chown=fetchnow:fetchnow src ./src
COPY --chown=fetchnow:fetchnow migrations ./migrations

USER fetchnow

# Late ARG so revision changes cannot reuse a stale LABEL from an earlier layer.
ARG FETCHNOW_RELEASE_REVISION=local
LABEL org.opencontainers.image.revision="${FETCHNOW_RELEASE_REVISION}"

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/live', timeout=3)"

CMD ["fetchnow-api"]
