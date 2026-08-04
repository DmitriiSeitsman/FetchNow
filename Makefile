.PHONY: setup up down logs lint format typecheck test build check migrate migration compose-check staging-config \
	pg-backup-test pg-backup-integration pg-backup-create pg-backup-verify pg-backup-list pg-backup-prune-dry \
	release-test release-preflight release-health release-health-integration release-ancestry-integration \
	release-build-test release-prepare release-verify release-build-integration \
	release-rollout-test release-rollout release-recover release-rollout-integration

COMPOSE ?= docker compose
BACKEND ?= backend
WEB ?= web
STAGING_COMPOSE ?= $(COMPOSE) --env-file .env.staging --project-name fetchnow-staging -f compose.yaml -f compose.staging.yaml
PYTHON ?= python3.12
PG_BACKUP ?= $(PYTHON) scripts/fetchnow_pg_backup_cli.py
RELEASE ?= $(PYTHON) scripts/fetchnow_release_cli.py

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
	$(BACKEND)/.venv/bin/ruff check scripts/fetchnow_pg_backup scripts/fetchnow_release tests/pg_backup tests/release

format:
	cd $(BACKEND) && .venv/bin/ruff format src tests
	cd $(BACKEND) && .venv/bin/ruff check --fix src tests
	cd $(WEB) && npm run format
	$(BACKEND)/.venv/bin/ruff format scripts/fetchnow_pg_backup scripts/fetchnow_release tests/pg_backup tests/release
	$(BACKEND)/.venv/bin/ruff check --fix scripts/fetchnow_pg_backup scripts/fetchnow_release tests/pg_backup tests/release

typecheck:
	cd $(BACKEND) && .venv/bin/mypy src
	cd $(WEB) && npm run typecheck

test:
	cd $(BACKEND) && .venv/bin/pytest -q
	$(BACKEND)/.venv/bin/pytest -q tests/pg_backup tests/release

build:
	cd $(WEB) && npm run build
	$(COMPOSE) build

compose-check:
	python3 scripts/compose_contract_check.py

pg-backup-test:
	$(BACKEND)/.venv/bin/pytest -q tests/pg_backup

pg-backup-integration:
	$(PYTHON) scripts/pg_backup_integration_test.py

pg-backup-create:
	@test -n "$(BACKUP_ROOT)" || (echo 'Usage: make pg-backup-create BACKUP_ROOT=/srv/fetchnow-staging/backups' && exit 1)
	@test -f .env.staging || (echo "Missing .env.staging" && exit 1)
	$(PG_BACKUP) create \
		--project-name fetchnow-staging \
		--env-file .env.staging \
		--compose-file compose.yaml \
		--compose-file compose.staging.yaml \
		--backup-root "$(BACKUP_ROOT)"

pg-backup-verify:
	@test -n "$(BACKUP_ROOT)" || (echo 'Usage: make pg-backup-verify BACKUP_ROOT=... BACKUP_ID=...' && exit 1)
	@test -n "$(BACKUP_ID)" || (echo 'Usage: make pg-backup-verify BACKUP_ROOT=... BACKUP_ID=...' && exit 1)
	@test -f .env.staging || (echo "Missing .env.staging" && exit 1)
	$(PG_BACKUP) verify \
		--project-name fetchnow-staging \
		--env-file .env.staging \
		--compose-file compose.yaml \
		--compose-file compose.staging.yaml \
		--backup-root "$(BACKUP_ROOT)" \
		--backup-id "$(BACKUP_ID)"

pg-backup-list:
	@test -n "$(BACKUP_ROOT)" || (echo 'Usage: make pg-backup-list BACKUP_ROOT=...' && exit 1)
	$(PG_BACKUP) list --backup-root "$(BACKUP_ROOT)"

pg-backup-prune-dry:
	@test -n "$(BACKUP_ROOT)" || (echo 'Usage: make pg-backup-prune-dry BACKUP_ROOT=...' && exit 1)
	$(PG_BACKUP) prune --backup-root "$(BACKUP_ROOT)" --keep $${KEEP:-7}

release-test:
	$(BACKEND)/.venv/bin/pytest -q tests/release

release-ancestry-integration:
	$(PYTHON) scripts/release_ancestry_integration_test.py

release-preflight:
	@test -f .env.staging || (echo "Missing .env.staging" && exit 1)
	@test -n "$(EXPECTED_REVISION)" || (echo 'Usage: make release-preflight EXPECTED_REVISION=<40-char-sha>' && exit 1)
	$(RELEASE) preflight \
		--project-name fetchnow-staging \
		--env-file .env.staging \
		--compose-file compose.yaml \
		--compose-file compose.staging.yaml \
		--expected-revision "$(EXPECTED_REVISION)"

release-health:
	@test -f .env.staging || (echo "Missing .env.staging" && exit 1)
	@test -n "$(EXPECTED_REVISION)" || (echo 'Usage: make release-health EXPECTED_REVISION=<40-char-sha>' && exit 1)
	$(RELEASE) health \
		--project-name fetchnow-staging \
		--env-file .env.staging \
		--compose-file compose.yaml \
		--compose-file compose.staging.yaml \
		--expected-revision "$(EXPECTED_REVISION)" \
		--gateway-base-url "$${GATEWAY_BASE_URL:-http://127.0.0.1:8091}"

release-health-integration:
	$(PYTHON) scripts/release_health_integration_test.py

release-build-test:
	$(BACKEND)/.venv/bin/pytest -q tests/release/test_release_build_unit.py

release-prepare:
	@test -f "$(ENV_FILE)" || (echo 'Usage: make release-prepare EXPECTED_REVISION=<sha> ENV_FILE=.env.staging DEPLOY_ROOT=/srv/fetchnow-staging' && exit 1)
	@test -n "$(EXPECTED_REVISION)" || (echo 'Usage: make release-prepare EXPECTED_REVISION=<sha> ENV_FILE=.env.staging DEPLOY_ROOT=/srv/fetchnow-staging' && exit 1)
	@test -n "$(DEPLOY_ROOT)" || (echo 'Usage: make release-prepare EXPECTED_REVISION=<sha> ENV_FILE=.env.staging DEPLOY_ROOT=/srv/fetchnow-staging' && exit 1)
	$(RELEASE) prepare \
		--project-name fetchnow-staging \
		--env-file "$(ENV_FILE)" \
		--compose-file compose.yaml \
		--compose-file compose.staging.yaml \
		--expected-revision "$(EXPECTED_REVISION)" \
		--deploy-root "$(DEPLOY_ROOT)"

release-verify:
	@test -n "$(EXPECTED_REVISION)" || (echo 'Usage: make release-verify EXPECTED_REVISION=<sha> DEPLOY_ROOT=/srv/fetchnow-staging' && exit 1)
	@test -n "$(DEPLOY_ROOT)" || (echo 'Usage: make release-verify EXPECTED_REVISION=<sha> DEPLOY_ROOT=/srv/fetchnow-staging' && exit 1)
	$(RELEASE) verify-release \
		--expected-revision "$(EXPECTED_REVISION)" \
		--deploy-root "$(DEPLOY_ROOT)"

release-build-integration:
	$(PYTHON) scripts/release_build_integration_test.py

release-rollout-test:
	$(BACKEND)/.venv/bin/pytest -q tests/release/test_rollout_unit.py

release-rollout:
	@test -n "$(EXPECTED_REVISION)" || (echo 'Usage: make release-rollout EXPECTED_REVISION=<sha> ENV_FILE=.env.staging DEPLOY_ROOT=/srv/fetchnow-staging [BOOTSTRAP=1]' && exit 1)
	@test -f "$(ENV_FILE)" || (echo 'Usage: make release-rollout EXPECTED_REVISION=<sha> ENV_FILE=.env.staging DEPLOY_ROOT=/srv/fetchnow-staging [BOOTSTRAP=1]' && exit 1)
	@test -n "$(DEPLOY_ROOT)" || (echo 'Usage: make release-rollout EXPECTED_REVISION=<sha> ENV_FILE=.env.staging DEPLOY_ROOT=/srv/fetchnow-staging [BOOTSTRAP=1]' && exit 1)
	$(RELEASE) rollout \
		--project-name fetchnow-staging \
		--env-file "$(ENV_FILE)" \
		--expected-revision "$(EXPECTED_REVISION)" \
		--deploy-root "$(DEPLOY_ROOT)" \
		$(if $(filter 1,$(BOOTSTRAP)),--bootstrap)

release-recover:
	@test -n "$(DEPLOYMENT_ID)" || (echo 'Usage: make release-recover DEPLOYMENT_ID=<uuid> ACTION=rollback|accept-target ENV_FILE=.env.staging DEPLOY_ROOT=/srv/fetchnow-staging' && exit 1)
	@test -n "$(ACTION)" || (echo 'Usage: make release-recover DEPLOYMENT_ID=<uuid> ACTION=rollback|accept-target ENV_FILE=.env.staging DEPLOY_ROOT=/srv/fetchnow-staging' && exit 1)
	@test -f "$(ENV_FILE)" || (echo 'Usage: make release-recover DEPLOYMENT_ID=<uuid> ACTION=rollback|accept-target ENV_FILE=.env.staging DEPLOY_ROOT=/srv/fetchnow-staging' && exit 1)
	@test -n "$(DEPLOY_ROOT)" || (echo 'Usage: make release-recover DEPLOYMENT_ID=<uuid> ACTION=rollback|accept-target ENV_FILE=.env.staging DEPLOY_ROOT=/srv/fetchnow-staging' && exit 1)
	$(RELEASE) recover \
		--project-name fetchnow-staging \
		--env-file "$(ENV_FILE)" \
		--deploy-root "$(DEPLOY_ROOT)" \
		--deployment-id "$(DEPLOYMENT_ID)" \
		--action "$(ACTION)"

release-rollout-integration:
	$(PYTHON) scripts/release_rollout_integration_test.py

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
