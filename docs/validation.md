# Validation and automation

## Local checks

Start the development database and load the same environment used by Django:

```sh
uv sync --locked
docker compose up -d --wait db
uv run --locked --env-file .env python manage.py migrate
uv run --locked --env-file .env make check
make compose-smoke
make security-check
```

`make check` runs Ruff lint/format checks, Django tests against PostgreSQL, uv
lockfile validation, Django database/system checks, migration drift detection,
and Compose configuration validation. The database role must be able to create
and delete Django's `test_<POSTGRES_DB>` database. Never point tests at an
investigation or shared hosted database.

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

The suite uses synthetic content and temporary storage. It covers account and
case roles, revoked access and cross-case requests, UUID/schema constraints,
immutable originals and provenance, private downloads, edit conflicts, confirmed
deletion and rollback, duplicate-safe submissions, bounded imports, and worker
publication/recovery. Tests live with their owning apps; see the domain guides
for the behavior those tests protect.

`make compose-smoke` needs Docker only. It builds a unique disposable project with
synthetic configuration, random loopback ports, and fresh volumes. It checks
migrations, production static delivery, retained records/original bytes, automatic
native extraction, bounded PDF rendering, local OCR/plate search, and queue persistence/execution across
restarts. A probe completes once in the controlled run; this does not establish
exactly-once delivery after crashes. The script clears inherited `PANDECT_`
settings and never uses your `.env` or data. Its exit trap removes only its own
containers and volumes.

`make security-check` exports the hash-locked dependency graph, including developer
tools, and runs pip-audit against it without re-resolving dependencies. It requires
internet access to the advisory service. Temporary exports are removed on exit.

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
the browser modules listed below through Django with `config.test_settings`, separately
from the ordinary unit suite. Browser fixtures use synthetic accounts/material
and temporary private file storage. Never run competing Django test processes against the same
test database, or point the suite at an investigation database.

`PANDECT_BROWSER_ENGINES` selects `chromium`, `webkit`, or both (comma-separated;
default both). `PANDECT_BROWSER_ARTIFACTS` overrides captures for the shared
harness and marketing; map captures always use `data/validation/maps`. Defaults
are listed below. Artifacts are ignored local output; keep fixtures synthetic.

The suites cover current roles and access removal, JavaScript upload recovery,
no-JavaScript workflows, keyboard focus, responsive reflow, enlarged text,
light/dark appearance, reduced motion, and forced colors. The core journeys include
confirmed deletion/import inventories, duplicate review, and the evidence reader.
Long originals remain complete, findings follow the original, and header **Info**
and **More** panels stay keyboard accessible. Info holds metadata and processing
history; More holds editing, archive/deletion, and additional analysis. Checks
cover panel dismissal/focus return, plus focused check-submission errors without
JavaScript. Shared-shell checks cover Cases, Case Tools, Evidence, Processing,
People, Vehicles, Findings, Settings, and Account at desktop/mobile sizes, tooltip
hover/focus/Escape behavior, enlarged text, dark appearance, and forced colors.

| Suite | Main scope | Default captures |
| --- | --- | --- |
| `config.tests.browser` | Accounts, cases, collaborators, people/vehicles, evidence and imports | `data/validation/browser` |
| `phone.imazing_browser` | iMazing CSV preparation, reviewed people, publication and no-JavaScript import | `data/validation/imazing-browser` |
| `marketing.browser` | Public navigation and FAQ disclosures | `data/validation/marketing-browser` |
| `core.analysis.browser` | Case Tools, Processing, extraction, reports, source links and review | `data/validation/analysis-browser` |
| `core.analysis.context_browser` | Local OCR and retained geographic context | `data/validation/context-browser` |
| `core.analysis.workflow_browser` | Complete findings, person links, connections, chronology and processing | `/private/tmp/pandect-workflow-browser` when explicitly configured |
| `core.analysis.selection_browser` | Shared selections, portable briefs and mocked question synthesis | Shared browser artifact directory |
| `document.browser` | UTF-8/PDF originals and private recording playback | Shared browser artifact directory |
| `phone.browser` | Multi-account iMessage mapping without JavaScript | Shared browser artifact directory |
| `image.browser` | Synthetic plate search and private crops | `data/validation/plate-browser` |
| `geo.map_browser` | Map, POIs and keyboard source navigation | `data/validation/maps` |

Inspect captures at the viewport sizes and density targets in [DESIGN.md](../DESIGN.md).
Manual screen-reader testing is optional and does not block delivery. Successful
builds or HTTP readiness do not establish these interactions, full accessibility
compliance, recognition accuracy, or live deployment health.

## Investigation workflow boundaries

The ordinary PostgreSQL suite covers latest-equivalent finding selection,
independent finding/result reviews, person attribution, exact shared selections,
import membership, processing recovery, chronology, conservative identifier keys,
and source/prior-finding synthesis quotations. Inference is mocked in routine
tests; use [separate runtime qualification](local-analysis.md)
for model/runtime evidence.

To reproduce the findings resource benchmark and populated-database backfill check:

```sh
uv run --locked --env-file .env python manage.py test core.analysis.tests.test_catalogue_bounds core.analysis.tests.test_catalogue_migrations --settings=config.test_settings --noinput
```

The benchmark exercises 25 displayed runs with 500 file inputs each, reports time
and query/file-check counts, and rejects full result-payload reads. The migration
check starts with retained populated and empty results, applies the additive
catalogue migrations, and verifies preservation without opening originals. The
catalogue unit tests separately cover interrupted backfill recovery. Selection
tests exercise 500 findings from one large result and multiple large extraction
members to prevent repeated loads or retained unused payloads.

## Person POI validation

`make check` covers POI validation, same-case links, roles, stale edits, deletion,
and bounded private map results. `geo.map_browser` exercises pointer/keyboard
creation, validation recovery, editing, and no-JavaScript person-linked workflows
in both engines. Tile responses are mocked; tests do not establish physical
location accuracy or live tile-service coverage.

## Image preview validation

Image evidence preview checks cover current case roles and revoked access,
private headers, unchanged original downloads, orientation, resizing, transparency,
malformed/active content, byte/pixel bounds, and safe failures. Run the focused
checks with:

```sh
uv run --locked --env-file .env python manage.py test --settings=config.test_settings image.tests.test_previews --noinput
uv run --locked --env-file .env python manage.py test --settings=config.test_settings config.tests.browser.CoreInterfaceBrowserTests.test_image_evidence_preview_and_download --noinput
```

The preview journey uses a synthetic multi-picture JPEG. Regressions also cover
48 MP progressive JPEGs, unchanged original bytes, and rejection above 50 million
pixels. The context browser uses a 48 MP JPEG with real local OCR. These fixtures
do not qualify arbitrary photos or native RAW files.

## Analysis validation

`make check` includes retained-content search, reports, processing/review history,
native imports, text/conversation/location methods, and inference boundaries.
The analysis and context journeys are included in `make browser-check`; run a
focused suite by passing its module to the same Django test command.

Processing unit tests live with their domain apps; shared execution, access,
publication, and result integration tests remain in `core.analysis.tests`.
For a focused parser check, run:

```sh
uv run --locked --env-file .env python manage.py test --settings=config.test_settings core.analysis.tests.test_parser_dispatch document.tests.test_analysis image.tests.test_analysis image.tests.test_ocr image.tests.test_plates core.analysis.tests.test_plates audio.tests.test_analysis geo.tests.test_geographic_context --noinput
```

Parser tests use real bounded child processes with synthetic documents, images,
audio, geographic data, and local printed OCR. Host validation requires Tesseract
and English data; see [development](development.md#run-python-on-the-host).
Inference and geographic HTTP responses are mocked in ordinary tests. Worker
checks exercise same-database dispatch, concurrent idempotent submissions,
publication and provenance; they do not establish general OCR/model accuracy.

## Local inference validation

Local inference boundary coverage is included in `make check`: routing and
immutable execution snapshots, pinned-runtime identity, context/output budgets,
authorization, cancellation, safe HTTP failures, and private native-runtime
authentication/limits. The runtime tests use synthetic model bytes and temporary
loopback listeners, including real process-group termination. They do not load
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
  CI publishes the Linux amd64 web and worker image targets to GHCR. CD resolves
  their successful-run and commit tags to matching digests and deploys the native
  Dokploy Applications, verifying worker startup before the web release. See
  [continuous deployment](operations.md#continuous-deployment). After both services
  pass verification, a dependent job publishes a GitHub release for the tested
  commit. Stale skips and failed deployments do not publish.
- **Scheduled security audit** reruns the dependency audit weekly and on demand.
- **CodeQL** analyzes Python on pushes, pull requests, and a weekly schedule;
  the workflow guard skips private repositories.

Workflows use pinned action commits and default to read-only contents permissions.
Only CI's production image jobs receive package-write permission; CD's deploy job
receives package-read permission and the production environment's deployment
secrets. Its separate release job receives contents-write permission through the
built-in GitHub token, without deployment secrets. These workflows do not perform
container or secret scanning.

The deployment and release helpers have mocked API and failure-path tests without
credentials, network access, or a database:

```sh
uv run --locked python -m unittest config.tests.test_dokploy_deploy config.tests.test_github_release
```

These tests cover immutable deployment identity, worker-before-web ordering,
bounded failures, stale skips, release reruns and tag conflicts. GitHub permissions,
GHCR/Dokploy access, and actual release publication require live verification.

Local checks do not prove hosted workflow execution or deployed behavior. Hosted
checks run only after changes reach GitHub.

## Public release checks

Review and scan the exact source and Git refs intended for publication for
credentials, private infrastructure details, and investigation material. Review
retained issues, pull requests, attachments, Actions logs, and artifacts before
changing an existing repository's visibility. Confirm that the source license,
third-party notices, and private reporting instructions cover the release.
Verify CodeQL and the repository's secret scanning and push protection settings
after a visibility change.

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
