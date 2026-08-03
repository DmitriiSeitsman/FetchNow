.PHONY: setup up down logs lint format typecheck test build check migrate migration compose-check staging-config

COMPOSE ?= docker compose
BACKEND ?= backend
WEB ?= web
STAGING_COMPOSE ?= $(COMPOSE) --env-file .env.staging --project-name fetchnow-staging -f compose.yaml -f compose.staging.yaml

setup:
	@command -v python3.12 >/dev/null || (echo "python3.12 is required" && exit 1)
	python3.12 -m venv $(BACKEND)/.venv
	$(BACKEND)/.venv/bin/pip install --upgrade pip
	$(BACKEND)/.venv/bin/pip install -e "$(BACKEND)[dev]"
	cd $(WEB) && npm install
	@echo "Setup complete. Copy backend/.env.example and web/.env.example as needed."

up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=200

lint:
	cd $(BACKEND) && .venv/bin/ruff check src tests
	cd $(WEB) && npm run lint

format:
	cd $(BACKEND) && .venv/bin/ruff format src tests
	cd $(BACKEND) && .venv/bin/ruff check --fix src tests
	cd $(WEB) && npm run format

typecheck:
	cd $(BACKEND) && .venv/bin/mypy src
	cd $(WEB) && npm run typecheck

test:
	cd $(BACKEND) && .venv/bin/pytest -q

build:
	cd $(WEB) && npm run build
	$(COMPOSE) build

compose-check:
	python3 scripts/compose_contract_check.py

staging-config:
	@test -f .env.staging || (echo "Missing .env.staging — copy from .env.staging.example" && exit 1)
	$(STAGING_COMPOSE) config >/dev/null
	@echo "Staging compose config OK"

check: lint typecheck test compose-check
	cd $(WEB) && npm run build

migrate:
	$(COMPOSE) run --rm api alembic upgrade head

migration:
	@test -n "$(m)" || (echo 'Usage: make migration m="message"' && exit 1)
	$(COMPOSE) run --rm api alembic revision -m "$(m)"
