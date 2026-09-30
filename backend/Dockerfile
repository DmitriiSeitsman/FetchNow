# syntax=docker/dockerfile:1

FROM python:3.14.7-slim-trixie AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /uvx /bin/

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./

# Frozen lock is the install. Do not follow this with `pip install .`.
RUN uv lock --check \
    && uv sync --frozen --no-dev --no-editable --python /usr/local/bin/python

FROM python:3.14.7-slim-trixie AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    APP_HOME=/app \
    TEMP_STORAGE_PATH=/var/lib/fetchnow/tmp

# ffmpeg/ffprobe: Debian trixie official apt package (not compiled here).
# Version token is the distro package; worker logs never dump `--version` text.
# OpenSSL: pin the trixie-security revision that closes CVE-2026-75804 /
# CVE-2026-84782 (libssl3t64, openssl, openssl-provider-legacy together).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        libssl3t64=3.5.7-1~deb13u3 \
        openssl=3.5.7-1~deb13u3 \
        openssl-provider-legacy=3.5.7-1~deb13u3 \
    && rm -rf /var/lib/apt/lists/*

# SEC-03C: base image ships ensurepip's pip with vendored msgpack/setuptools
# (not app uv.lock packages; not present under /opt/venv). Runtime never invokes
# pip — remove the system pip install + ensurepip wheel so those copies are gone.
# Do not touch /opt/venv.
RUN /usr/local/bin/python -m pip uninstall -y pip \
    && rm -rf /usr/local/lib/python3.14/ensurepip \
    && rm -f /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.14

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

# ---------------------------------------------------------------------------
# test: Candidate A runtime + frozen --extra dev for CI backend acceptance.
# Not a release/runtime image. Dev deps are layered here only.
# ---------------------------------------------------------------------------
FROM runtime AS test

USER root

COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /uvx /bin/

# build-essential only for compiling any non-wheel extras during uv sync.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY pyproject.toml uv.lock ./
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Extend the Candidate A runtime venv with frozen test/dev deps (same uv.lock).
RUN uv sync --frozen --extra dev --python /usr/local/bin/python

COPY tests ./tests

RUN chown -R fetchnow:fetchnow /opt/venv /build

USER fetchnow

ENV PYTHONPATH=/build/src \
    PATH="/opt/venv/bin:$PATH" \
    HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

# Differs from runtime CMD ["fetchnow-api"]: CI overrides the command with pytest.
# No docker --init / tini (matches canonical runtime, which also has no init wrapper).
