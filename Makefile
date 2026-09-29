SHELL := /bin/sh

COMPOSE ?= docker compose
DOCKER ?= docker
PANDECT_UID ?= $(shell id -u)
PANDECT_GID ?= $(shell id -g)
TOOLS_COMPOSE = $(COMPOSE) --profile tools
TOOLS_RUN = $(TOOLS_COMPOSE) run --rm --no-deps tools
LOCK_RUN = ./scripts/with-maintenance-lock.sh
PYTHON_SOURCES = manage.py pandect tests scripts
CLOUDFLARE_LICENSE_ARG = $(if $(filter yes,$(ACCEPT_META_LICENSE)),--accept-meta-license,)

export PANDECT_UID
export PANDECT_GID
export BACKUP_DIR
export BACKUP

# Validation is also safe before setup: checkouts sharing a basename must not
# overwrite each other's images. Compose still interprets explicit .env values.
ifeq ($(shell LC_ALL=C grep -Eq '^[[:space:]]*(export[[:space:]]+)?COMPOSE_PROJECT_NAME[[:space:]]*=' .env 2>/dev/null && echo configured),)
COMPOSE_PROJECT_NAME ?= pandect-$(shell printf '%s' "$$(pwd -P)" | cksum | awk '{print $$1 "-" $$2}')
export COMPOSE_PROJECT_NAME
endif

.DEFAULT_GOAL := help

.PHONY: help setup up down restart logs ps shell manage migrate makemigrations task-smoke processing-backfill processing-diagnostics models-provision model-smoke cloudflare-smoke test lint format validate check clean backup restore security-check compose-smoke _tools-image

help: ## List public commands and their state-changing behaviour.
	@awk 'BEGIN {FS = ":.*## "; print "Pandect commands:"} /^[a-zA-Z0-9_-]+:.*## / {printf "  %-16s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

setup: ## [mutates] Create local config/data, build the image, and apply migrations.
	@./scripts/setup.sh

up: ## [mutates] Start the isolated media runner, web, and worker services.
	$(LOCK_RUN) $(COMPOSE) up --detach

down: ## [mutates] Stop services without deleting persistent data.
	$(LOCK_RUN) $(COMPOSE) down --remove-orphans

restart: ## [mutates] Restart application and media-runner services.
	$(LOCK_RUN) $(COMPOSE) restart media-runner web worker

logs: ## Follow application and media-runner logs.
	$(COMPOSE) logs --follow web worker media-runner

ps: ## Show service and health status.
	$(COMPOSE) ps

shell: ## [interactive; may mutate data] Open a Django shell in a one-off container.
	$(LOCK_RUN) $(COMPOSE) run --rm --no-deps web python manage.py shell

manage: ## [mutates depending on ARGS] Run manage.py ARGS="..." in a one-off container.
	@test -n "$(ARGS)" || { echo 'Usage: make manage ARGS="command [options]"' >&2; exit 2; }
	$(LOCK_RUN) $(COMPOSE) run --rm --no-deps web python manage.py $(ARGS)

migrate: ## [mutates] Apply database migrations explicitly.
	$(LOCK_RUN) $(COMPOSE) run --rm --no-deps web python manage.py migrate --noinput

makemigrations: _tools-image ## [mutates source/Docker cache] Create migrations.
	$(TOOLS_RUN) python manage.py makemigrations

task-smoke: ## [mutates] Verify enqueue-to-worker execution with a bounded wait.
	$(LOCK_RUN) $(COMPOSE) run --rm --no-deps web python manage.py task_smoke --timeout 10

processing-diagnostics: ## Inspect local processing tools, models, and optional Cloudflare configuration.
	$(LOCK_RUN) $(COMPOSE) run --rm --no-deps web python manage.py processing_diagnostics

models-provision: ## [network/mutates data] Provision pinned models; requires ACCEPT_MODEL_LICENSES=yes.
	@test "$(ACCEPT_MODEL_LICENSES)" = "yes" || { echo 'Review with make manage ARGS="provision_processing_models --list"; then set ACCEPT_MODEL_LICENSES=yes.' >&2; exit 2; }
	$(LOCK_RUN) $(COMPOSE) --profile models run --rm --no-deps model-provisioner python manage.py provision_processing_models --accept-licenses $(ARGS)

processing-backfill: ## [mutates] Backfill bounded items; pass ARGS="--item-type image --limit 20".
	@test -n "$(ARGS)" || { echo 'Usage: make processing-backfill ARGS="item-id or --item-type TYPE [--limit N]"' >&2; exit 2; }
	$(LOCK_RUN) $(COMPOSE) run --rm --no-deps web python manage.py backfill_processing $(ARGS)

model-smoke: ## Run bounded evidence-free inference with provisioned local models.
	$(LOCK_RUN) $(COMPOSE) run --rm --no-deps web python manage.py processing_diagnostics --smoke-models

cloudflare-smoke: ## [network/cost] Check Cloudflare; set ACCEPT_META_LICENSE=yes only after reviewing Meta terms.
	$(LOCK_RUN) $(COMPOSE) run --rm --no-deps worker python manage.py processing_diagnostics --cloudflare $(CLOUDFLARE_LICENSE_ARG)

backup: ## [mutates backup destination] Back up a stopped stack; BACKUP_DIR is required.
	@test -n "$${BACKUP_DIR}" || { echo 'Usage: make backup BACKUP_DIR=/private/backup/path' >&2; exit 2; }
	@./scripts/maintenance.sh backup "$${BACKUP_DIR}"

restore: ## [mutates empty data destination] Restore a verified backup; BACKUP is required.
	@test -n "$${BACKUP}" || { echo 'Usage: make restore BACKUP=/private/backups/pandect-backup-ID' >&2; exit 2; }
	@./scripts/maintenance.sh restore "$${BACKUP}"

security-check: ## [network/Docker cache] Audit dependencies, source history, and runtime image without evidence or credentials.
	@./scripts/security-check.sh

compose-smoke: ## [mutates disposable test state] Exercise a clean, isolated Compose installation and recovery.
	@./scripts/compose-smoke.sh

_tools-image:
	$(TOOLS_COMPOSE) build tools

test: _tools-image ## [mutates Docker cache] Run the hermetic pytest suite.
	$(TOOLS_RUN) pytest -p no:cacheprovider

lint: _tools-image ## [mutates Docker cache] Run Ruff formatting and lint checks.
	$(TOOLS_RUN) ruff format --check $(PYTHON_SOURCES)
	$(TOOLS_RUN) ruff check $(PYTHON_SOURCES)

format: _tools-image ## [mutates source/Docker cache] Format files with Ruff.
	$(TOOLS_RUN) ruff format $(PYTHON_SOURCES)

validate: _tools-image ## [mutates Docker cache] Validate Django, migrations, Compose, and the runtime image.
	$(TOOLS_RUN) python manage.py check
	$(TOOLS_RUN) python manage.py makemigrations --check --dry-run
	$(COMPOSE) config --quiet
	$(COMPOSE) build web
	$(DOCKER) run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges --entrypoint python "$$($(COMPOSE) config --images web | sort -u)" -c "import cv2, openvino, sherpa_onnx; print('Media runtime imports passed.')"

check: lint test validate ## [mutates Docker cache] Run the complete CI validation suite.

clean: ## [mutates] Remove disposable caches/build output; preserve .env and data.
	@./scripts/clean.sh
