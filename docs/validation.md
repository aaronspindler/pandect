# Validation and automation

## Choosing checks

Choose coverage using the [contributor requirements](../AGENTS.md#validation-and-handoff)
and, for visible changes, [DESIGN.md](../DESIGN.md#completion-and-design-review):

- Application changes: run `make check`; visible changes also need the affected
  browser journeys.
- Container, startup, or asset packaging changes: also run `make compose-smoke`.
- Dependency changes: also run `make security-check`.
- Documentation-only changes: check source accuracy, links, and whitespace;
  application tests that restate prose are unnecessary.

Complete the required [simplicity review](README.md#simplicity-review) after
validation. Report local checks, hosted CI, and live verification separately.

## Local checks

Use the [local configuration](development.md#configuration) with a disposable
PostgreSQL database. The database role must be able to create and delete
`test_<POSTGRES_DB>`. Keep investigation and shared hosted databases separate.

```sh
uv sync --locked
docker compose up -d --wait db
uv run --locked --env-file .env python manage.py migrate
uv run --locked --env-file .env make check
```

`make check` combines:

- `make static-check` runs repository hygiene, Ruff lint and format checks,
  `uv lock --check`, and Compose configuration validation.
- `make test` runs the PostgreSQL tests with `config.test_settings`.
- `make database-check` runs Django's database/system checks and checks for
  missing migrations.

`make validate` runs configuration and database checks. `make format` rewrites
files. Direct test commands need `--settings=config.test_settings`. Override
Make's module with `TEST_SETTINGS=<module>` for another test configuration.
Routine tests use synthetic fixtures and storage, source assets, and mocked
provider transport. Test settings clear live provider destinations and credentials.

`make compose-smoke` needs Docker with Compose v2 and host `python3`. It uses a
temporary project, fresh volumes, random loopback ports, and synthetic
configuration. It exercises the non-debug static manifest, shared web/worker
image, startup, private storage, processing and worker recovery. See the
[smoke script](../scripts/compose-smoke.sh). Use
`sh scripts/compose-smoke.sh <local-image>` to test a prebuilt local image by ID.
The database and optional proxy images can still be pulled.

With the development Chromium installed,
`PANDECT_SMOKE_WORKSPACE_BROWSER=1 make compose-smoke` exercises job updates
through a temporary nginx proxy under concurrent uploads, downloads and streams.
It checks a two-second local p95 target. Use the
[hosted rollout checks](operations.md#first-dokploy-deployment) for the actual
ingress and deployment.

`make security-check` needs advisory-service internet access. It uses pip-audit
on the hash-locked Python graph, including developer dependencies.
For the Cloudflare Worker, follow the [local checks](cloudflare-agent.md#configure-the-private-route).

## Repository hygiene

Pandect's repository is public. Never commit/push local test evidence, including
synthetic imports, exports, and browser artifacts. Keep it under ignored `data/`
or outside the checkout; generate fixtures in code. Static UI assets and reviewed
design illustrations are source assets, never evidence storage.

Install the versioned Git hooks in every clone before creating commits or pushing:

```sh
make install-hooks
make repository-check
```

The installer sets `core.hooksPath` to `.githooks`; reconcile existing custom hooks
first. Pre-commit checks the whole staged tree, including force-added ignored
paths. Pre-push checks every outgoing branch/tag's history: later deletion of
evidence does not unblock a push. Branch/tag deletions are allowed. Both hooks
use `python3`, without Django settings or a database.

The checker rejects runtime/evidence directories, environment files except the
two root examples, dumps, document/message/location exports, archives, recordings,
and images outside static/design directories. It reports categories/counts without
reading or printing evidence contents or filenames. `make repository-check`
checks the index and locally reachable history. CI fetches full history and runs
it through `make static-check`. Shallow clones require `git fetch --unshallow` first.

Run the Git integration regression tests without a database:

```sh
uv run --locked python -m unittest config.tests.test_repository_hygiene
```

Checks recognize paths/formats, not private content in source text or allowed
assets. Review the exact staged diff and outgoing commits. Hooks can be bypassed;
CI runs after upload. On failure, keep material local and remove it from every
outgoing commit before pushing; later deletion is insufficient. Do not force-push
or rewrite shared history as routine validation.

## Evidence Chat qualification

Use synthetic evidence, a disposable PostgreSQL database and an isolated search
namespace. Exercise two cases, revoked access, duplicate remote acknowledgements,
conflicting statements, an unsupported conclusion and an exact passage in a long
transcript. Compare a fresh installation and an upgrade with saved citations.
See [the Agent integration](cloudflare-agent.md) and [search lifecycle](cloudflare-search.md).

## Browser checks

Run the live-server suites with the same isolated PostgreSQL setup:

```sh
uv sync --locked
uv run --locked playwright install chromium webkit
uv run --locked --env-file .env make browser-check
```

On Linux, also install browser OS dependencies with
`uv run --locked playwright install --with-deps chromium webkit`. The
[Makefile](../Makefile) owns the suite list. `PANDECT_BROWSER_ENGINES` accepts
`chromium`, `webkit`, or `chromium,webkit` (the default). The four Instagram
collector modules use Chromium only. The monitor journey runs on both engines.

Capture defaults live with the test modules. `PANDECT_BROWSER_ARTIFACTS`
overrides paths for the shared harness and marketing within
`data/validation/`. Paths and symlinks escaping that directory are rejected.
Map captures use `data/validation/maps`. Keep artifacts local and synthetic.

Inspect affected journeys and captures at [design review viewports](../DESIGN.md#completion-and-design-review).
Use accessible roles and named regions in tests and wait for the expected
navigation or content before checking results. Manual screen-reader testing
requires an explicit request.

## Focused checks

Pass an owning app's module, class, or method for focused checks. Browser modules
use the same command and isolated database setup:

```sh
uv run --locked --env-file .env python manage.py test --settings=config.test_settings image.tests.test_previews --noinput
uv run --locked --env-file .env python manage.py test --settings=config.test_settings config.tests.browser.CoreInterfaceBrowserTests.test_image_evidence_preview_and_download --noinput
```

When changing isolation boundaries, verify with synthetic records in separate
cases, including two cases the same investigator can access. Cover account-only
settings, case roles and revocation, normal and enhanced page loads, search and
live activity, private file reads and exports, and worker retries or publication.
Check both allowed results and rejected foreign references. Browser checks
should exercise account changes and navigational history in one browser context.

For the offline Instagram pilot, exercise PostgreSQL, fresh worker processes, and
a loopback-only Chromium service with three scheduled checks against generated
lists. Run from the checkout with the locked development browser installed:

```sh
uv run --locked --env-file .env env POSTGRES_HOST=127.0.0.1 POSTGRES_DB=pandect_instagram_qualification python manage.py test socialmedia.pilot_integration --settings=config.test_settings --noinput
```

The test uses a disposable database and private media. It checks case approval,
queue isolation, a saved delivery consumed after a worker restart, additions at
unchanged counts, a quiet check, original checksums, revocation, disconnect, and
case deletion. It advances due times in the test database. The
[live release gate](instagram-monitors.md#live-release-gate) requires three
actual daily checks on a qualified source.

The Chromium browser suite verifies signed-in observer identity, profile
discovery, and endpoint pagination against locally fulfilled pages in
`socialmedia.live_roster_browser`, `socialmedia.endpoint_profile_browser`, and
`socialmedia.endpoint_roster_browser`. The separate
[one-time live check](instagram-monitors.md#one-controlled-check-on-a-mac)
uses a private browser and a controlled account.

Shared execution and publication tests live in `core.analysis.tests`; concrete
processing tests live in their domain apps. For a focused parser check:

```sh
uv run --locked --env-file .env python manage.py test --settings=config.test_settings core.analysis.tests.test_parser_dispatch document.tests.test_analysis image.tests.test_analysis image.tests.test_ocr image.tests.test_plates core.analysis.tests.test_plates audio.tests.test_analysis geo.tests.test_geographic_context --noinput
```

To reproduce the findings resource benchmark and populated-database backfill check:

```sh
uv run --locked --env-file .env python manage.py test core.analysis.tests.test_catalogue_bounds core.analysis.tests.test_catalogue_migrations --settings=config.test_settings --noinput
```

### Hosted search and inference

Routine tests mock Cloudflare and use PostgreSQL. A provider qualification must
use disposable case instances and generated evidence. Verify OCR page and region
anchors and recording timestamps against the fixture originals. Exercise an
approved external model and Workers AI separately and record their actual
routing, usage and citation outcomes.

## GitHub Actions

The [CI workflow](../.github/workflows/ci.yml) always classifies changes, uses
full Git history for repository hygiene, and runs static and automation contract
checks. Full runs also exercise:

- Fresh PostgreSQL migrations, application tests, database validation and Compose
  smoke on native Linux AMD64 and ARM64 runners.
- Chromium and WebKit journeys in separate Linux jobs with isolated PostgreSQL.
- Local installation, tests and bundling of the Cloudflare investigation Worker.
- The locked Python dependency audit. A [separate workflow](../.github/workflows/security.yml)
  runs it weekly and on demand.

The workflow owns uv cache settings and native BuildKit cache scopes. Smoke
uses the image built locally in CI. [CD](operations.md#continuous-deployment)
builds again from the tested source and uses the matching architecture's cache.
The release checks below cover public image scans. Set up GitHub-managed CodeQL
for repositories that use it and verify it in security settings. GitHub
[rejects advanced workflow uploads while default setup is enabled](https://docs.github.com/en/code-security/reference/code-scanning/troubleshoot-analysis-errors/two-codeql-workflows).

Deployment and release helpers have offline mocked tests covering identity, ordering,
failures and version conflicts:

```sh
uv run --locked python -m unittest config.tests.test_dokploy_deploy config.tests.test_github_release
```

Treat local checks, hosted CI and live deployment verification as separate evidence.

### Documentation-only runs

CI always reports **CI status**. Static checks and automation contract tests
still run for documentation-only changes. The eligible paths are Markdown under
`docs/` and the root `README.md`, `AGENTS.md`, `DESIGN.md`, `PRINCIPLES.md`,
and `SECURITY.md`. The final check accepts skipped application jobs only after
successful explicit classification.

For pull requests, CI checks changes from the merge base to the PR head including
both sides of a rename. For upstream `main`, it starts at the tested commit in
the newest explicit successful `pandect-rollout` deployment record for
`production`. That must be the latest rollout and on current first-parent
history. CI checks intermediate changes even if later reverted. Missing records,
uncertain rollouts, forced updates, unavailable history and Git/API errors
require full coverage. Manual runs always select full coverage.

[CD preflight](operations.md#continuous-deployment) requires the explicit
validation decision and checks the triggering attempt before production
credentials are available. Confirmed documentation-only changes skip image
publication, rollout, and release.

## Public release checks

The repository is public. For every contribution or release, review and scan the
exact source and Git refs intended for publication for credentials, private
infrastructure details, and investigation material. Review
retained issues, pull requests, attachments, Actions logs, and artifacts for the
same material. Confirm that the source license, third-party notices, and private
reporting instructions cover the release.
Verify CodeQL and the repository's secret scanning and push protection settings
for the public repository.

For public image distribution, record each platform image digest and its source
revision. Inspect the image's package inventory and provenance, review applicable
redistribution terms for the base image and bundled libraries, and retain
required notices and corresponding source where applicable. Run a container
vulnerability scan on each exact digest and review the results before publication.
Repeat these checks for separately distributed inference images and model artifacts.

References: [GitHub visibility consequences](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/managing-repository-settings/setting-repository-visibility)
and [GitHub secret scanning](https://docs.github.com/en/code-security/concepts/secret-security/secret-scanning).

## Dependabot

The [Dependabot configuration](../.github/dependabot.yml) checks uv, Docker,
npm, and GitHub Actions daily. uv, Docker and Actions group version updates,
with up to five open PRs per ecosystem. npm checks the Cloudflare Worker
separately. Review security-update settings in GitHub.

uv updates both `pyproject.toml` and `uv.lock`, including developer dependencies.
The Docker group excludes Python image updates and PostgreSQL major upgrades.
Follow the [coordinated runtime pins](architecture.md#runtime-decisions) for
Python changes and plan a data migration for PostgreSQL major upgrades.
