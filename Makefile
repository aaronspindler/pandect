UV ?= uv
RUN = $(UV) run --locked

.PHONY: sync run lint format test validate check security-check compose-smoke browser-check

sync:
	$(UV) sync --locked

run:
	docker compose up --build --wait

lint:
	$(RUN) ruff check .
	$(RUN) ruff format --check .

format:
	$(RUN) ruff check --fix .
	$(RUN) ruff format .

test:
	$(RUN) python manage.py test

browser-check:
	$(RUN) python manage.py test config.tests.browser config.marketing_browser analysis.browser analysis.context_browser cases.map_browser --noinput

validate:
	$(UV) lock --check
	$(RUN) python manage.py check --database default
	$(RUN) python manage.py makemigrations --check --dry-run
	docker compose config --quiet

check: lint test validate

# Audit the resolved graph without pip resolving a different set of packages.
security-check:
	@set -eu; requirements=$$(mktemp); trap 'rm -f "$$requirements"' EXIT; \
	$(UV) export --locked --no-emit-project --format requirements-txt > "$$requirements"; \
	$(RUN) pip-audit --require-hashes --disable-pip -r "$$requirements"

compose-smoke:
	sh scripts/compose-smoke.sh
