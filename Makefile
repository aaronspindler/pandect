SHELL := /bin/sh

COMPOSE ?= docker compose
DOCKER ?= docker
PANDECT_UID ?= $(shell id -u)
PANDECT_GID ?= $(shell id -g)
TOOLS_COMPOSE = $(COMPOSE) --profile tools
TOOLS_RUN = $(TOOLS_COMPOSE) run --rm --no-deps tools
PYTHON_SOURCES = manage.py pandect tests scripts
CLOUDFLARE_LICENSE_ARG = $(if $(filter yes,$(ACCEPT_META_LICENSE)),--accept-meta-license,)

export PANDECT_UID
export PANDECT_GID

.DEFAULT_GOAL := help

.PHONY: help setup up down restart logs ps shell manage migrate makemigrations task-smoke processing-backfill processing-diagnostics models-provision model-smoke cloudflare-smoke test lint format validate check clean _tools-image

help: ## List public commands and their state-changing behaviour.
	@awk 'BEGIN {FS = ":.*## "; print "Pandect commands:"} /^[a-zA-Z0-9_-]+:.*## / {printf "  %-16s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

setup: ## [mutates] Create local config/data, build the image, and apply migrations.
	@./scripts/setup.sh

up: ## [mutates] Start the web and worker services in the background.
	$(COMPOSE) up --detach

down: ## [mutates] Stop services without deleting persistent data.
	$(COMPOSE) down --remove-orphans

restart: ## [mutates] Restart the web and worker services.
	$(COMPOSE) restart web worker

logs: ## Follow web and worker logs.
	$(COMPOSE) logs --follow web worker

ps: ## Show service and health status.
	$(COMPOSE) ps

shell: ## [interactive; may mutate data] Open a Django shell in a one-off container.
	$(COMPOSE) run --rm --no-deps web python manage.py shell

manage: ## [mutates depending on ARGS] Run manage.py ARGS="..." in a one-off container.
	@test -n "$(ARGS)" || { echo 'Usage: make manage ARGS="command [options]"' >&2; exit 2; }
	$(COMPOSE) run --rm --no-deps web python manage.py $(ARGS)

migrate: ## [mutates] Apply database migrations explicitly.
	$(COMPOSE) run --rm --no-deps web python manage.py migrate --noinput

makemigrations: _tools-image ## [mutates source/Docker cache] Create migrations.
	$(TOOLS_RUN) python manage.py makemigrations

task-smoke: ## [mutates] Verify enqueue-to-worker execution with a bounded wait.
	$(COMPOSE) run --rm --no-deps web python manage.py task_smoke --timeout 10

processing-diagnostics: ## Inspect local processing tools, models, and optional Cloudflare configuration.
	$(COMPOSE) run --rm --no-deps web python manage.py processing_diagnostics

models-provision: ## [network/mutates data] Provision pinned models; requires ACCEPT_MODEL_LICENSES=yes.
	@test "$(ACCEPT_MODEL_LICENSES)" = "yes" || { echo 'Review with make manage ARGS="provision_processing_models --list"; then set ACCEPT_MODEL_LICENSES=yes.' >&2; exit 2; }
	$(COMPOSE) --profile models run --rm --no-deps model-provisioner python manage.py provision_processing_models --accept-licenses $(ARGS)

processing-backfill: ## [mutates] Backfill bounded items; pass ARGS="--item-type image --limit 20".
	@test -n "$(ARGS)" || { echo 'Usage: make processing-backfill ARGS="item-id or --item-type TYPE [--limit N]"' >&2; exit 2; }
	$(COMPOSE) run --rm --no-deps web python manage.py backfill_processing $(ARGS)

model-smoke: ## Run bounded evidence-free inference with provisioned local models.
	$(COMPOSE) run --rm --no-deps web python manage.py processing_diagnostics --smoke-models

cloudflare-smoke: ## [network/cost] Check Cloudflare; set ACCEPT_META_LICENSE=yes only after reviewing Meta terms.
	$(COMPOSE) run --rm --no-deps web python manage.py processing_diagnostics --cloudflare $(CLOUDFLARE_LICENSE_ARG)

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
	$(DOCKER) run --rm --entrypoint python pandect:local -c "import cv2, openvino, sherpa_onnx; print('Media runtime imports passed.')"

check: lint test validate ## [mutates Docker cache] Run the complete CI validation suite.

clean: ## [mutates] Remove disposable caches/build output; preserve .env and data.
	@./scripts/clean.sh
