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

Complete [local setup](development.md#first-run), then start the database and load
Django's environment:

```sh
uv sync --locked
docker compose up -d --wait db
uv run --locked --env-file .env python manage.py migrate
uv run --locked --env-file .env make check
make compose-smoke
make security-check
```

`make check` runs repository hygiene, Ruff lint/format, PostgreSQL tests, uv
lockfile validation, Django database/system checks, migration drift detection,
and Compose configuration validation. The database role must create/delete
`test_<POSTGRES_DB>`. Never use an investigation or shared hosted database.

`make lint`, `make test`, and `make validate` run individual groups; management
checks need the local `uv run --env-file .env` wrapper. `make format` applies Ruff
fixes and formatting.

Native Dokploy image deployment and the separate live web/database/worker checks
are documented in [the Dokploy deployment procedure](operations.md#first-dokploy-deployment).
The development Compose smoke does not validate public DNS, TLS, or a remote rollout.

`make test` and `make browser-check` select `config.test_settings`: PostgreSQL and
application settings with source assets, without a release manifest. Direct test
commands also need `--settings=config.test_settings`; override Make's module with
`TEST_SETTINGS=<module>` for a separate test database configuration. Static tests
exercise manifest collection/cache headers; Compose smoke checks the non-debug build.

Tests live with their apps, generate synthetic content in code, and use temporary
storage. Domain guides explain behavior; test modules own coverage inventories.

`make compose-smoke` needs only Docker. It uses synthetic configuration, random
loopback ports, and fresh volumes in a unique disposable project to exercise
startup, storage, processing, and worker restart. It runs production worker startup
and verifies the shared web/worker image. See
[`scripts/compose-smoke.sh`](../scripts/compose-smoke.sh) for coverage. One successful
probe does not establish exactly-once delivery after crashes. The script clears
inherited `PANDECT_` settings, ignores your `.env`/data, and removes only its own
containers/volumes on exit.

`make security-check` runs pip-audit on the hash-locked graph, including developer
tools, without re-resolving. It needs advisory-service internet access and removes
temporary exports on exit.

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
checks the index and locally reachable history; CI fetches full history and runs
it through `make check`. Shallow clones require `git fetch --unshallow` first.

Run the Git integration regression tests without a database:

```sh
uv run --locked python -m unittest config.tests.test_repository_hygiene
```

Checks recognize paths/formats, not private content in source text or allowed
assets. Review the exact staged diff and outgoing commits. Hooks can be bypassed;
CI runs after upload. On failure, keep material local and remove it from every
outgoing commit before pushing; later deletion is insufficient. Do not force-push
or rewrite shared history as routine validation.

## Browser checks

Install the locked developer tools and browser engines, then run the focused
live-server suite against the same isolated PostgreSQL test setup:

```sh
uv sync --locked
uv run --locked playwright install chromium webkit
uv run --locked --env-file .env make browser-check
```

On Linux, install Playwright's OS dependencies (CI uses
`playwright install --with-deps chromium webkit`). `make browser-check` runs the
[Makefile](../Makefile) modules with `config.test_settings`, separately from unit
tests, using synthetic accounts/material and temporary private storage. Never share
a test database between competing Django processes or use an investigation database.

`PANDECT_BROWSER_ENGINES` selects `chromium`, `webkit`, or both (comma-separated;
default both). `PANDECT_BROWSER_ARTIFACTS` overrides captures for the shared
harness and marketing; map captures always use `data/validation/maps`. Defaults
are listed below. Artifacts are ignored local output; keep fixtures synthetic.

| Suite | Default captures |
| --- | --- |
| `config.tests.browser` | `data/validation/browser` |
| `core.evidence.bulk_admin_browser` | `data/validation/browser` |
| Message CSV import | `data/validation/message-csv-browser` |
| `marketing.browser` | `data/validation/marketing-browser` |
| `core.analysis.browser` | `data/validation/analysis-browser` |
| `core.analysis.context_browser` | `data/validation/context-browser` |
| `core.analysis.workflow_browser` | `data/validation/investigation-workflows` |
| `core.analysis.reprocessing_browser` | `data/validation/reprocessing-browser` |
| `core.analysis.selection_browser` | `data/validation/selection-browser` |
| `document.browser` | `data/validation/original-preview-browser` |
| `phone.browser` | `data/validation/import-mapping-browser` |
| `phone.conversation_browser` | `data/validation/conversations-browser` |
| `image.browser` | `data/validation/plate-browser` |
| `geo.map_browser` | `data/validation/maps` |
| `ui.live_browser` | `data/validation/live-updates` |

Inspect captures at [DESIGN.md](../DESIGN.md) viewport/density targets. Manual
screen-reader testing requires an explicit user request and does not block
delivery. Builds or HTTP readiness do not establish interaction behavior, full
accessibility compliance, recognition accuracy, or live deployment health.

## Focused checks

Pass an owning app's module, class, or method for focused checks. Browser modules
use the same command and isolated database setup:

```sh
uv run --locked --env-file .env python manage.py test --settings=config.test_settings image.tests.test_previews --noinput
uv run --locked --env-file .env python manage.py test --settings=config.test_settings config.tests.browser.CoreInterfaceBrowserTests.test_image_evidence_preview_and_download --noinput
```

Shared execution and publication tests live in `core.analysis.tests`; concrete
processing tests live in their domain apps. For a focused parser check:

```sh
uv run --locked --env-file .env python manage.py test --settings=config.test_settings core.analysis.tests.test_parser_dispatch document.tests.test_analysis image.tests.test_analysis image.tests.test_ocr image.tests.test_plates core.analysis.tests.test_plates audio.tests.test_analysis geo.tests.test_geographic_context --noinput
```

To reproduce the findings resource benchmark and populated-database backfill check:

```sh
uv run --locked --env-file .env python manage.py test core.analysis.tests.test_catalogue_bounds core.analysis.tests.test_catalogue_migrations --settings=config.test_settings --noinput
```

Parser tests use bounded child processes with synthetic documents, images, audio,
geographic data, and local printed OCR. Host checks require
[Tesseract and English data](development.md#run-python-on-the-host). Routine tests
mock inference, geographic HTTP, and tiles; they do not establish general
recognition/model accuracy, physical location accuracy, or live coverage.

## Local inference validation

`make check` includes local inference boundary checks using synthetic model bytes
and temporary loopback listeners, without Whisper/Qwen weights. Run them with:

```sh
uv run --locked python -m unittest inference.test_server
uv run --locked --env-file .env python manage.py test --settings=config.test_settings core.analysis.tests.test_local_inference core.analysis.tests.test_ollama core.analysis.tests.test_execution config.tests.test_settings --noinput
```

Optional runtime build check:
`docker compose --profile local-ai build local-transcription`.
Builds do not provision models or prove quality. After provisioning reviewed
artifacts, follow [local qualification](local-analysis.md#qualification-and-recovery).
Record actual output, latency, and peak memory separately from mocked tests/builds;
block third-party egress while permitting private services. Never use public map
capacity in routine tests.

## GitHub Actions

- **CI** runs the same checks, fresh PostgreSQL migrations, and Compose smoke on
  Linux amd64 and arm64. uv reads `.python-version` and installs from `uv.lock`.
- A separate Linux amd64 browser job installs Chromium/WebKit and runs
  `make browser-check` for core, marketing, analysis, geographic context and map
  journeys with synthetic configuration and PostgreSQL.
- A separate CI job audits locked dependencies without database credentials.
- After those checks pass for a `main` push in the Pandect operator repository,
  CI publishes one Linux amd64 application image to GHCR. CD resolves its
  successful-run and commit tags to a matching digest and deploys that same image
  to the native web and worker Dokploy Applications with their respective startup
  commands, verifying worker startup before the web release. See
  [continuous deployment](operations.md#continuous-deployment). After both services
  pass verification, a dependent job publishes a GitHub release for the tested
  commit. Stale skips and failed deployments do not publish.
- **Scheduled security audit** reruns the dependency audit weekly and on demand.
- **CodeQL** uses GitHub-managed default setup in the repository's security
  settings. Upstream scans Python, JavaScript/TypeScript, and Actions with extended
  queries and remote/local threat models. Forks must configure their own setup.
  Keep advanced CodeQL workflow files out while default setup is enabled:
  [GitHub rejects their analysis uploads](https://docs.github.com/en/code-security/reference/code-scanning/troubleshoot-analysis-errors/two-codeql-workflows).

Workflows use pinned action commits and default to read-only contents permissions.
Only CI's production image job receives package-write permission; CD's deploy job
receives package-read permission and the production environment's deployment
secrets. Its separate release job receives contents-write permission through the
built-in GitHub token, without deployment secrets. These workflows do not perform
container or secret scanning.

The deployment and release helpers have mocked API and failure-path tests without
credentials, network access, or a database:

```sh
uv run --locked python -m unittest config.tests.test_dokploy_deploy config.tests.test_github_release
```

These cover immutable deployment identity, startup commands, worker-before-web
ordering, bounded failures, stale skips, release reruns, and tag conflicts. GitHub
permissions, GHCR/Dokploy access, and publication require live verification.
Hosted checks run after upload; local checks do not prove their execution or
deployed behavior.

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

The weekly configuration uses the native `uv`, `docker`, and `github-actions`
ecosystems. Each ecosystem groups version updates into one PR, retaining a limit
of five open PRs per ecosystem. Native uv updates maintain both `pyproject.toml`
and `uv.lock`, including the development group. Docker discovers the Dockerfile
and Compose images. Python image updates are excluded because the exact Python
pin, compatibility range, lockfile, and image must be upgraded together. PostgreSQL
major updates are excluded because they require a planned database upgrade.
Security updates follow GitHub's separate settings; these groups do not opt into
grouping security updates.

Review runtime updates against the coordinated pins in
[architecture](architecture.md); a Docker update alone does not update
`.python-version` or `mise.toml`. New uv and Django major versions may require
adjusting the declared compatibility ranges.

References: [uv and GitHub Actions](https://docs.astral.sh/uv/guides/integration/github/),
[uv and Dependabot](https://docs.astral.sh/uv/guides/integration/dependabot/).
