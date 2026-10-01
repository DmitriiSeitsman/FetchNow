# syntax=docker/dockerfile:1
#
# SEC-03C Candidate A native acceptance ONLY.
# Builds a disposable test image ON TOP OF an already-built Candidate A
# runtime image from backend/Dockerfile (no --target). Not a release image.
#
# Build context: repository root (so backend/tests and repo-root fixtures are
# visible; backend/.dockerignore intentionally keeps tests out of the
# production build context).
#
# Layout mirrors the git checkout that backend tests expect:
#   Path(__file__).parents[2] == <repo_root>
# when tests live at <repo_root>/backend/tests/*.py
# Do not COPY . , host venv, secrets, or caches.

ARG FETCHNOW_RUNTIME_IMAGE=fetchnow-api:__SET_FETCHNOW_RUNTIME_IMAGE_BUILD_ARG__
FROM ${FETCHNOW_RUNTIME_IMAGE}

USER root

COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /uvx /bin/

# build-essential only for compiling non-wheel extras during uv sync of --extra dev.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build/backend

COPY backend/pyproject.toml backend/uv.lock ./
COPY backend/src ./src
COPY backend/migrations ./migrations
COPY backend/alembic.ini ./
COPY backend/tests ./tests

# Repo-root fixtures read via Path(__file__).resolve().parents[2] from backend/tests.
COPY deploy/nginx/nginx.conf /build/deploy/nginx/nginx.conf
COPY scripts/fetchnow_release/config_contract.py \
    /build/scripts/fetchnow_release/config_contract.py
COPY tests/fixtures/filename_adversarial_corpus.json \
    /build/tests/fixtures/filename_adversarial_corpus.json

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Extend the inherited Candidate A runtime venv with frozen test/dev deps.
# Same uv.lock as runtime; does not replace the base Python or OS libraries.
# --no-editable matches the runtime install into /opt/venv (not a live /build
# source checkout). Do not set PYTHONPATH.
RUN uv sync --frozen --extra dev --no-editable --python /usr/local/bin/python \
    && mkdir -p /tmp/pytest-cache /tmp/pytest-out \
    && chown -R fetchnow:fetchnow /opt/venv /build /tmp/pytest-cache /tmp/pytest-out \
    && rm -f /bin/uv /bin/uvx

USER fetchnow

ENV PATH="/opt/venv/bin:$PATH" \
    HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build/backend

# Differs from runtime CMD ["fetchnow-api"]: CI overrides the command with pytest.
# No docker --init / tini (matches canonical runtime, which also has no init wrapper).
