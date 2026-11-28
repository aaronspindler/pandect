UV ?= uv
RUN = $(UV) run --locked
TEST_SETTINGS ?= config.tests.settings
INSTAGRAM_COLLECTOR_TESTS = socialmedia.tests.pilot_browser socialmedia.tests.live_roster_browser socialmedia.tests.endpoint_profile_browser socialmedia.tests.endpoint_roster_browser
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
	$(RUN) python manage.py test config.tests.browser marketing.tests.browser core.analysis.tests.browser core.analysis.tests.context_browser core.analysis.tests.workflow_browser core.analysis.tests.sighting_browser core.analysis.tests.reprocessing_browser core.case_search.tests.browser core.evidence_chat.tests.browser core.evidence.tests.bulk_admin_browser core.evidence.tests.packet_browser core.tests.important_browser image.tests.browser document.tests.browser audio.tests.browser phone.tests.imessage_browser phone.tests.call_history_browser phone.tests.conversation_browser text.tests.claim_comparison_browser text.tests.writing_browser geo.tests.map_browser geo.tests.journey_claim_browser socialmedia.tests.monitor_browser $(INSTAGRAM_COLLECTOR_TESTS) ui.tests.live_browser ui.tests.workspace_browser ui.tests.dialog_browser --noinput --settings=$(TEST_SETTINGS)

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
