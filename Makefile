UV ?= uv
RUN = $(UV) run --locked
TEST_SETTINGS ?= config.test_settings
INSTAGRAM_COLLECTOR_TESTS = socialmedia.pilot_browser socialmedia.live_roster_browser socialmedia.endpoint_profile_browser socialmedia.endpoint_roster_browser
ifeq ($(PANDECT_BROWSER_ENGINES),webkit)
INSTAGRAM_COLLECTOR_TESTS =
endif

.PHONY: sync run lint format test configuration-check database-check static-check validate check security-check compose-smoke browser-check repository-check install-hooks

sync:
	$(UV) sync --locked

run:
	docker compose build
	@services=$$(docker compose config --services) && \
	set -- worker && \
	if printf '%s\n' "$$services" | grep -qx instagram-worker; then \
		set -- "$$@" instagram-worker; \
	fi && \
	docker compose stop --timeout -1 "$$@"
	docker compose up --no-build --wait

lint:
	$(RUN) ruff check .
	$(RUN) ruff format --check .

format:
	$(RUN) ruff check --fix .
	$(RUN) ruff format .

test:
	$(RUN) python manage.py test --settings=$(TEST_SETTINGS)

browser-check:
	$(RUN) python manage.py test config.tests.browser marketing.browser core.analysis.browser core.analysis.context_browser core.analysis.workflow_browser core.analysis.sighting_browser core.analysis.reprocessing_browser core.case_search.browser core.evidence_chat.browser core.evidence.bulk_admin_browser core.evidence.packet_browser core.important_browser image.browser document.browser audio.browser phone.imessage_browser phone.call_history_browser phone.conversation_browser text.claim_comparison_browser text.writing_browser geo.map_browser geo.journey_claim_browser socialmedia.monitor_browser $(INSTAGRAM_COLLECTOR_TESTS) ui.live_browser ui.workspace_browser ui.dialog_browser --noinput --settings=$(TEST_SETTINGS)

configuration-check:
	$(UV) lock --check
	docker compose config --quiet

database-check:
	$(RUN) python manage.py check --database default
	$(RUN) python manage.py makemigrations --check --dry-run

static-check: repository-check lint configuration-check

validate: configuration-check database-check

repository-check:
	$(RUN) python scripts/check-repository-hygiene.py --staged
	$(RUN) python scripts/check-repository-hygiene.py --history

install-hooks:
	git config --local core.hooksPath .githooks

check: static-check test database-check

# Audit the resolved graph without pip resolving a different set of packages.
security-check:
	@set -eu; requirements=$$(mktemp); trap 'rm -f "$$requirements"' EXIT; \
	$(UV) export --locked --no-emit-project --format requirements-txt > "$$requirements"; \
	$(RUN) pip-audit --require-hashes --disable-pip -r "$$requirements"

compose-smoke:
	sh scripts/compose-smoke.sh
