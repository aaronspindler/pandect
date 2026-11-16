# Validation and automation

## Choosing checks

Use the [contributor requirements](../AGENTS.md#validation-and-handoff) to select
behavior and failure-path coverage, and [DESIGN.md](../DESIGN.md#completion-and-design-review)
for visible changes. The commands below provide the execution procedures:

- Application changes: run `make check`; visible changes also need the affected
  browser journeys.
- Container, startup, or asset packaging changes: also run `make compose-smoke`.
- Dependency changes: also run `make security-check`.
- Documentation-only changes: check source accuracy, links, and whitespace;
  application tests that restate prose are unnecessary.

Complete the required [simplicity review](README.md#simplicity-review) after
validation. Report local checks, hosted CI, and live verification separately.

## Local checks

Complete [local setup](development.md#first-run), then start the development
database and load the same environment used by Django:

```sh
uv sync --locked
docker compose up -d --wait db
uv run --locked --env-file .env python manage.py migrate
uv run --locked --env-file .env make check
make compose-smoke
make security-check
```

`make check` runs repository hygiene checks, Ruff lint/format checks, Django tests
against PostgreSQL, uv lockfile validation, Django database/system checks,
migration drift detection, and Compose configuration validation. The database
role must be able to create and delete Django's `test_<POSTGRES_DB>` database.
Never point tests at an investigation or shared hosted database.

`make lint`, `make test`, and `make validate` run individual groups. Management
checks need environment variables; use the `uv run --env-file .env` wrapper when
calling them locally. `make format` applies Ruff fixes and formatting.

Native Dokploy image deployment and the separate live web/database/worker checks
are documented in [the Dokploy deployment procedure](operations.md#first-dokploy-deployment).
The development Compose smoke does not validate public DNS, TLS, or a remote rollout.

`make test` and `make browser-check` select `config.test_settings`, which keeps
PostgreSQL and application settings while serving source static assets without a
release manifest. Direct `python manage.py test` commands also need
`--settings=config.test_settings`, as shown below. Use `TEST_SETTINGS=<module>` with Make
when a separate test database configuration is needed. Static regression tests
exercise real manifest collection and cache headers independently; the Compose
smoke also checks the non-debug build.

Tests generate synthetic content in code, use temporary storage, and live with
their owning apps. The domain guides explain the behavior they protect; test
modules own the detailed coverage inventory.

`make compose-smoke` needs Docker only. It builds a unique disposable project with
synthetic configuration, random loopback ports, and fresh volumes to exercise
startup, storage, processing, and worker restart. It runs the production worker
startup script and verifies that web and worker use the same built image. The
executable coverage lives in
[`scripts/compose-smoke.sh`](../scripts/compose-smoke.sh). A probe completes once
in the controlled run; this does not establish
exactly-once delivery after crashes. The script clears inherited `PANDECT_`
settings and never uses your `.env` or data. Its exit trap removes only its own
containers and volumes.

`make security-check` exports the hash-locked dependency graph, including developer
tools, and runs pip-audit against it without re-resolving dependencies. It requires
internet access to the advisory service. Temporary exports are removed on exit.

## Repository hygiene

Pandect's source repository is public. Local test evidence must never be committed
or pushed, including synthetic imports, exports, and browser artifacts. Keep it
under ignored `data/` or outside the checkout. Tests generate their fixtures in
code and write files only to temporary/ignored storage. Static UI assets and
reviewed design illustrations remain source assets; never put evidence there.

Install the versioned Git hooks in every clone before creating commits or pushing:

```sh
make install-hooks
make repository-check
```

The installer sets this clone's `core.hooksPath` to `.githooks`; reconcile any
existing custom hooks first. The pre-commit hook checks the complete staged tree,
including force-added ignored paths. The pre-push hook checks the history of
every outgoing branch or tag, so committing an evidence file and later deleting
it still blocks the push. Branch/tag deletions carry no files and are allowed.
Both hooks use `python3` and require no Django settings or database.

The shared checker rejects runtime/evidence directories, environment files other
than the two root examples, database dumps, document/message/location exports,
archives, recordings, and images outside static/design asset directories. It
reports categories and counts without reading or printing evidence contents or
filenames. `make repository-check` checks the index and all locally reachable
history; CI fetches full history and runs it through `make check`.
History checks refuse shallow clones; run `git fetch --unshallow` before checking
or pushing from one.

Run the Git integration regression tests without a database:

```sh
uv run --locked python -m unittest config.tests.test_repository_hygiene
```

These checks recognize paths and file formats, not whether arbitrary source text
or an allowed asset contains private material. Review the exact staged diff and
outgoing commits. Hooks can be bypassed, and CI runs after upload; neither is a
substitute for keeping local evidence out of the source tree. If a check fails,
keep the material local and remove it from every outgoing commit before pushing;
a later deletion is insufficient. Do not force-push or rewrite shared history as
routine validation.

## Browser checks

Install the locked developer tools and browser engines, then run the focused
live-server suite against the same isolated PostgreSQL test setup:

```sh
uv sync --locked
uv run --locked playwright install chromium webkit
uv run --locked --env-file .env make browser-check
```

On Linux, install browser OS dependencies as required by Playwright (CI uses
`playwright install --with-deps chromium webkit`). `make browser-check` runs
the modules selected in the [Makefile](../Makefile) through Django with
`config.test_settings`, separately from the ordinary unit suite. Browser fixtures
use synthetic accounts/material and temporary private file storage. Never run
competing Django test processes against the same test database, or point the
suite at an investigation database.

`PANDECT_BROWSER_ENGINES` selects `chromium`, `webkit`, or both (comma-separated;
default both). `PANDECT_BROWSER_ARTIFACTS` overrides captures for the shared
harness and marketing; map captures always use `data/validation/maps`. Defaults
are listed below. Artifacts are ignored local output; keep fixtures synthetic.

| Suite | Default captures |
| --- | --- |
| `config.tests.browser` | `data/validation/browser` |
| Message CSV import | `data/validation/message-csv-browser` |
| `marketing.browser` | `data/validation/marketing-browser` |
| `core.analysis.browser` | `data/validation/analysis-browser` |
| `core.analysis.context_browser` | `data/validation/context-browser` |
| `core.analysis.workflow_browser` | `data/validation/investigation-workflows` |
| `core.analysis.selection_browser` | `data/validation/selection-browser` |
| `document.browser` | `data/validation/original-preview-browser` |
| `phone.browser` | `data/validation/import-mapping-browser` |
| `phone.conversation_browser` | `data/validation/conversations-browser` |
| `image.browser` | `data/validation/plate-browser` |
| `geo.map_browser` | `data/validation/maps` |

Inspect captures at the viewport sizes and density targets in [DESIGN.md](../DESIGN.md).
Manual screen-reader testing is optional and does not block delivery. Successful
builds or HTTP readiness do not establish these interactions, full accessibility
compliance, recognition accuracy, or live deployment health.

## Focused checks

Pass an owning app's test module, class, or method to Django to run a focused
check. Browser modules use the same command and isolated database setup:

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

Parser tests use real bounded child processes with synthetic documents, images,
audio, geographic data, and local printed OCR. Host validation requires Tesseract
and English data; see [development](development.md#run-python-on-the-host).
Inference, geographic HTTP responses, and map tiles are mocked in routine tests.
Passing these checks does not establish general recognition/model accuracy,
physical location accuracy, or live service coverage.

## Local inference validation

Local inference boundary checks are included in `make check`. Runtime tests use
synthetic model bytes and temporary loopback listeners; they do not load
Whisper or Qwen weights. Run their focused checks independently with:

```sh
uv run --locked python -m unittest inference.test_server
uv run --locked --env-file .env python manage.py test --settings=config.test_settings core.analysis.tests.test_local_inference config.tests.test_settings --noinput
```

Optional runtime build validation is
`docker compose --profile local-ai build local-text local-transcription`.
Building the image does not provision models or prove inference quality. Use
the executable qualification command and per-method evaluation procedure in
[local analysis](local-analysis.md#qualification-and-recovery) after provisioning
reviewed artifacts. Record actual output, latency and peak memory separately
from mocked-test and container-build results; block third-party egress while
permitting the private services. Do not use public map capacity in routine tests.

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

These tests cover shared immutable deployment identity, service startup commands,
worker-before-web ordering, bounded failures, stale skips, release reruns and tag
conflicts. GitHub permissions, GHCR/Dokploy access, and actual release publication
require live verification.

Local checks do not prove hosted workflow execution or deployed behavior. Hosted
checks run only after changes reach GitHub.

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
