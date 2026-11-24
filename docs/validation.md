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

CI runs `make static-check` once for repository hygiene, Ruff, lock validation,
and Compose configuration. `make database-check` runs Django's database/system
checks and migration drift detection. `make check` still includes both groups
and the full PostgreSQL test suite; `make validate` retains configuration and
database validation.

Native Dokploy image deployment and the live web, event listener, database, and worker checks
are documented in [the Dokploy deployment procedure](operations.md#first-dokploy-deployment).
The development Compose smoke does not validate public DNS, TLS, or a remote rollout.

`make test` and `make browser-check` select `config.test_settings`: PostgreSQL and
application settings with source assets, without a release manifest. Test settings
clear live gateway destinations and external AI credentials. Compose smoke also
uses isolated synthetic configuration. Provider tests supply synthetic settings
and mock HTTP transport. Direct test commands also need
`--settings=config.test_settings`; override Make's module with
`TEST_SETTINGS=<module>` for a separate test database configuration. Static tests
exercise manifest collection/cache headers; Compose smoke checks the non-debug build.
Synthetic test users use a fast password hasher only in test settings. A focused
authentication regression selects production hashers and exercises successful
and failed sign-in; production password settings are unchanged.

Tests live with their apps, generate synthetic content in code, and use temporary
storage. Domain guides explain behavior; test modules own coverage inventories.

`make compose-smoke` needs Docker with Compose v2 and a host `python3` on
`PATH`. It uses synthetic configuration, random loopback ports, and fresh
volumes in a disposable project to exercise startup, storage, processing, and
worker restart. It runs production worker startup and verifies the shared
worker and web image. The non-debug HTTP probe requests
assets through the production manifest and checks their content type and immutable
caching. See [`scripts/compose-smoke.sh`](../scripts/compose-smoke.sh) for coverage. One successful
probe does not establish exactly-once delivery after crashes. The script clears
inherited `PANDECT_` settings, ignores your `.env`/data, and removes only its own
containers/volumes on exit.

With the project's Playwright Chromium installed, run
`PANDECT_SMOKE_WORKSPACE_BROWSER=1 make compose-smoke` to measure queued,
running, and finished job badges through a temporary nginx proxy to the unified
ASGI web process. The probe holds a multipart upload and a private download open
while 30 browser contexts receive events. It checks the transfer contents, web
worker memory growth, early upload rejection, and that healthy streams stop polling.
Its local p95 target is two seconds. The deployment proxy needs a separate live check.

To exercise a prebuilt local image without rebuilding it, run
`sh scripts/compose-smoke.sh <local-image>`. The script resolves that reference
to an immutable local image ID, uses it for web and worker, and rejects
missing images or a running-image mismatch. Application images are never pulled
or rebuilt in this mode; PostgreSQL and the optional probe proxy image may still be pulled. The no-argument Make
target retains its normal local build. The application image's uv download cache
uses a BuildKit cache mount and is not included in the shipped image.

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

Use accessible roles for controls and named regions for displayed evidence or
limitations. Wait for the expected navigation before reading a redirected URL and
for initial rendering to settle before simulating a refresh or retry.

`PANDECT_BROWSER_ENGINES` selects `chromium`, `webkit`, or both (comma-separated,
default both). The four Instagram collection modules listed under
`INSTAGRAM_COLLECTOR_TESTS` in the Makefile use Chromium and are omitted from
the WebKit-only target. The shared Instagram monitor journey runs on both
engines. `PANDECT_BROWSER_ARTIFACTS` overrides captures for the shared
harness and marketing within the checkout's `data/validation/` directory. Paths
and symlinks resolving outside that directory are rejected. Map captures always
use `data/validation/maps`. Defaults are listed below. Artifacts are ignored local
output; keep fixtures synthetic.

| Suite | Default captures |
| --- | --- |
| `config.tests.browser` | `data/validation/browser` |
| `core.evidence.bulk_admin_browser` | `data/validation/browser` |
| `core.evidence.packet_browser` | `data/validation/evidence-packet` |
| Message CSV import | `data/validation/message-csv-browser` |
| `marketing.browser` | `data/validation/marketing-browser` |
| `core.analysis.browser` | `data/validation/analysis-browser` |
| `core.analysis.context_browser` | `data/validation/context-browser` |
| `core.analysis.workflow_browser` | `data/validation/investigation-workflows` |
| `core.analysis.sighting_browser` | `data/validation/sighting-browser` |
| `core.analysis.reprocessing_browser` | `data/validation/reprocessing-browser` |
| `core.case_search.browser` | `data/validation/case-search` |
| `core.evidence_chat.browser` | `data/validation/evidence-chat` |
| `document.browser` | `data/validation/original-preview-browser` |
| `audio.browser` | `data/validation/m4a-recording-browser` |
| `phone.conversation_browser` | `data/validation/conversations-browser` |
| `image.browser` | `data/validation/plate-browser` |
| `geo.map_browser` | `data/validation/maps` |
| `geo.journey_claim_browser` | `data/validation/plan-comparisons` |
| `socialmedia.monitor_browser` | `data/validation/instagram-monitors` |
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

For case-search qualification, configure the four
[private embedding settings](local-analysis.md#private-case-search-embeddings)
for an operator-controlled model reachable from the test process. Run
`uv run --locked --env-file .env python manage.py test core.case_search.qualification --settings=config.test_settings --noinput`.
This opt-in suite uses a disposable Django database. It checks keyword and
semantic retrieval on paraphrases, distractors and unrelated queries, and
times the real worker on the small corpus. It measures exact UUID lookup,
selective and high-frequency keyword matches, live suggestions, spelling
correction and the complete case-search response with private query embedding. The large fixture contains 250,000
independent records with repeated synthetic filler text and its matching vector.
It measures retrieval and database footprint separately from model throughput.
Set `PANDECT_QUALIFY_PASSAGES` to vary the scale.

### Hosted meaning search

With the gateway and its stored OpenAI key alias configured, run the following
opt-in check from the application environment. It creates a disposable test
database, approves a synthetic case, indexes its generated evidence and posts
queries through the ordinary results page. It makes billable embedding
requests and checks linked excerpts, retrieval, unsupported queries and usage.
Routine tests use mocked inference.

```sh
uv run --locked --env-file .env python manage.py test core.case_search.hosted_qualification --settings=config.test_settings --noinput
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

- **CI** first classifies changes and runs architecture-independent static
  checks once, with full history for repository hygiene. uv reads
  `.python-version` and installs from `uv.lock`.
- Full application runs retain fresh PostgreSQL migrations, tests, database
  validation, and Compose smoke on native Linux amd64 and arm64 runners.
- Separate Linux amd64 Chromium and WebKit jobs run their respective
  [browser suites](#browser-checks) with independent PostgreSQL services and
  synthetic configuration.
- A separate application-run job audits locked dependencies without database
  credentials. The scheduled audit also continues independently of source changes.
- CI and the scheduled audit share uv dependency caches keyed by `.python-version`,
  `pyproject.toml`, and `uv.lock`; setup-uv also separates the runner OS/version and
  architecture. Source-only changes retain reuse, while runtime/dependency changes
  invalidate the cache. The static job seeds amd64 before its dependent jobs;
  only the arm64 check and the independent scheduled/manual audit additionally
  save caches. Downstream amd64 jobs restore only. Every job still installs or
  audits the locked graph; a cache hit never skips validation. See
  [setup-uv caching](https://github.com/astral-sh/setup-uv/blob/68559e99cc807c1eb7e9238fd43a15bba460eec2/docs/caching.md).
- Native smoke builds and production publication reuse architecture-specific
  GitHub Actions BuildKit layer caches. Only upstream `main` CI smoke builds and
  CD builds export those caches; PR and manual CI runs restore only. Cache misses
  use cold builds, and cache export failures do not hide build, smoke, or
  publication failures.
  Smoke exercises the exact locally loaded image; publication rebuilds from the
  tested source using the matching architecture's layer cache.
  The Dockerfile installs dependencies before copying source and sets release
  metadata after static collection, so source/release changes retain earlier
  layers. Its uv cache mount is local to the builder and is not exported by the
  [GitHub Actions layer cache](https://docs.docker.com/build/ci/github-actions/cache/#cache-mounts).
  Browser binaries and host apt packages are installed fresh; Playwright
  [does not recommend browser caching](https://playwright.dev/python/docs/ci#caching-browsers)
  because restore/download times are comparable and Linux dependencies still need
  installation.
- Successful full CI for a `main` push in the operator repository is eligible
  for [continuous deployment](operations.md#continuous-deployment), which owns
  image publication, rollout, and release sequencing.
- **Scheduled security audit** reruns the dependency audit weekly and on demand.
- **CodeQL** uses GitHub-managed default setup in the repository's security
  settings. Upstream scans Python, JavaScript/TypeScript, and Actions with extended
  queries and remote/local threat models. Forks must configure their own setup.
  Keep advanced CodeQL workflow files out while default setup is enabled:
  [GitHub rejects their analysis uploads](https://docs.github.com/en/code-security/reference/code-scanning/troubleshoot-analysis-errors/two-codeql-workflows).

Workflows use pinned action commits and default to read-only contents permissions.
CD's native build, combined-image, and final release jobs receive package-write
permission; the final job uses it to promote the verified version tag. The deploy
job receives package-read permission and the production environment's deployment
secrets. Preflight receives Actions read permission to inspect CI results, and
release preparation receives contents-read permission, without production
secrets. The final release job also receives contents-write permission through
the built-in GitHub token, without deployment secrets. These workflows do not perform container or secret scanning.
CI's classification job receives deployment-read permission to check the latest
explicit production rollout before skipping application work. CD's production
job receives deployment-write permission to record that rollout's actual tested
commit and outcome; repository content writes stay isolated in the release job.

The deployment and release helpers have mocked API and failure-path tests without
credentials, network access, or a database:

```sh
uv run --locked python -m unittest config.tests.test_dokploy_deploy config.tests.test_github_release
```

These cover immutable deployment identity, startup commands, worker-before-web
ordering, bounded failures, ancestry guards, consecutive release allocation,
release reruns, and tag conflicts. GitHub permissions, GHCR/Dokploy access, and
publication require live verification.
Hosted checks run after upload; local checks do not prove their execution or
deployed behavior.

### Documentation-only runs

CI always starts and reports a final **CI status** check. Changes limited to
Markdown under `docs/` and the root `README.md`, `AGENTS.md`, `DESIGN.md`,
`PRINCIPLES.md`, and `SECURITY.md` can skip application tests, browsers, dependency
audit, and the subsequent CD image publication. Static checks and changed-range
whitespace checks still run. Review documentation accuracy and affected links
during handoff.
The final check accepts skipped jobs only after successful explicit classification;
unexpected skips, failures, and cancellations fail it. Requiring this status in
branch rules is an operator setting, not a workflow side effect.

PR classification compares the merge base with the PR head, including both
sides of renames. Upstream `main` uses the newest explicit GitHub deployment
record for task `pandect-rollout` in `production`. Its latest status must confirm
success, and its tested commit must lie on current first-parent history. The
classifier checks every changed path since that commit, including intermediate
application changes later reverted. Release publication time and the deployment
workflow's own source revision do not establish the deployed image's identity.

Missing records, pending or failed rollouts, forced updates, unavailable commit
history, and Git/API uncertainty require full CI. An older successful record
never substitutes for a newer uncertain rollout. The first main run after this
tracking is introduced therefore uses full coverage; its verified deployment
enables later documentation-only skips. No application database migration or new
service is required.
Manual runs always select full coverage. The classifier never changes Git refs.

CD's secret-free preflight requires successful static and final status jobs and
the status job's explicit successful validation decision. For a full application
run, it also requires successful native AMD64/ARM64 tests, Chromium/WebKit journeys,
and dependency audit before image publication and deployment. A confirmed
documentation-only decision skips all CD publication, deployment, and release
jobs; skipped matrix jobs need not have expanded into platform/browser entries.
Missing, failed, or ambiguous results fail preflight rather than being interpreted
as documentation-only work. Queued releases also pass the commit-ancestry checks
described in the operations guide.

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
pin, compatibility range, lockfile, and image must be upgraded together. The
major-update ignore rule matches `postgres`. The current Compose image is
`pgvector/pgvector`, so review the PostgreSQL variant in its tag separately
and plan any PostgreSQL major upgrade before accepting an image update.
Security updates follow GitHub's separate settings; these groups do not opt into
grouping security updates.

Review runtime updates against the coordinated pins in
[architecture](architecture.md); a Docker update alone does not update
`.python-version` or `mise.toml`. New uv and Django major versions may require
adjusting the declared compatibility ranges.

References: [uv and GitHub Actions](https://docs.astral.sh/uv/guides/integration/github/),
[uv and Dependabot](https://docs.astral.sh/uv/guides/integration/dependabot/).
