SHELL := /bin/sh

COMPOSE ?= docker compose
PANDECT_UID ?= $(shell id -u)
PANDECT_GID ?= $(shell id -g)
TOOLS_COMPOSE = $(COMPOSE) --profile tools
TOOLS_RUN = $(TOOLS_COMPOSE) run --rm --no-deps tools
PYTHON_SOURCES = manage.py pandect tests scripts

export PANDECT_UID
export PANDECT_GID

.DEFAULT_GOAL := help

.PHONY: help setup up down restart logs ps shell manage migrate makemigrations task-smoke test lint format validate check clean _tools-image

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

check: lint test validate ## [mutates Docker cache] Run the complete CI validation suite.

clean: ## [mutates] Remove disposable caches/build output; preserve .env and data.
	@./scripts/clean.sh
