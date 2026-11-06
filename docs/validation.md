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

Tests cover the custom user, password hashing, sign-in failure, email verification,
registration, CSRF, sign-out semantics, staff access, and database readiness and
failure. Case tests cover required ownership, stable creation timestamps,
collaborator roles and constraints, protected owners, and membership cleanup.
Identifier tests cover independent UUIDv4 defaults on every Pandect model and
native PostgreSQL UUID columns for their keys and all referencing foreign keys,
including Django/allauth and automatically generated join tables.
Admin tests cover model-permission gates, scoped lists and direct URLs (including
history and deletion), owner-only changes, unrelated superusers, revoked access,
CSRF, and forged ownership, membership IDs, parent cases, and roles.
Evidence tests cover all material types, required cases/uploaders, exact original
bytes and checksums, metadata bounds, immutable originals, historical attribution,
private storage paths, committed deletion cleanup, rollback, and storage failures.
Location coverage includes paired/ranged coordinates, original JSON/checksum
stability, same-case person selection, retained person links, whole-case deletion,
and source creation/edit/search across every evidence type.
Evidence admin coverage includes all ten evidence registrations, role/model-permission
gates, scoped searches and direct/history URLs, forged originals and attribution,
same-case person links, upload limits, archive attribution, superuser-only confirmed
individual deletion with original/call/derived-file cleanup, rollback, storage
failures, downstream analysis, import-source protection, and permission-checked
whole-case cleanup.
Communication coverage includes same-case sender/recipient links, immutable
message/time/duration originals, all initial call-content combinations, appended
transcripts/recordings, independent hashes, parent-scoped private downloads,
retry IDs, upload/storage failures, access revocation, person-deletion protection,
case cascade/rollback, and read-only admin attachments.
Product-view tests additionally cover current role gates and cross-case UUID
requests, private downloads, metadata conflicts, archive/restore, bounded uploads,
and idempotent retries after uncertain confirmation. In-app superuser deletion
covers current role/verification checks, all evidence types, retained call and
generated files, explicit confirmation, private pages, preserved navigation, CSRF,
case isolation, complete imported-source cascades, corrupt-link blocks, paginated
inventories/private downloads, stale or tampered confirmations, worker publication
after deletion, and rollback of every derived row and file. Duplicate-upload
coverage
checks exact hashes across file categories and captured text, archived matches,
case isolation, current edit access, bounded private match lists, explicit
confirmation, unchanged storage after warnings, and the full-page fallback.
Case/invitation tests cover
verified recipients, token rotation/replay/expiry, mail failure, preserved member
roles, confirmed deletion, and reviewed stale-edit resubmission.
They use synthetic content and temporary storage, without external services.
Background coverage checks the real PostgreSQL task backend in debug mode,
execution and recorded failure, same-transaction commit/rollback and visibility
from a second database connection, diagnostic success/failure/mismatch/timeout,
safe worker logs, and denial of global task admin access even to superusers.
Person tests cover partial identities, contact validation, case linkage and
deletion, timestamps, duplicate contacts, scoped lists/search/history, owner and
editor writes, read-only viewers, forged case selections, revoked/downgraded
memberships, model permissions, CSRF, and case-deletion permission requirements.
Vehicle tests cover partial identifiers, year/identity database constraints,
duplicate identifiers, scoped searches, role gates, forged case choices,
revocation, reviewed stale edits, deletion, and admin model permissions.
Vehicle-location CSV coverage includes source retention and checksum/provenance,
explicit timezone conversion and DST rejection, format/resource bounds, same-case
vehicle mappings, current roles and CSRF, exact retries, possible duplicate review,
atomic rollback, corrupted/unavailable sources, and retained-link deletion rules.
Background import coverage exercises a CSV larger than the former 1 MiB/250-row
limits, paginated review/results, upload vehicle defaults and forged choices,
queued processing and retries, current permissions, and atomic publication.
Duplicate coverage includes optional retention, skipping within and between
exports (including archived matches), new matches after review, all-skipped
completion, unchanged retained sources, and duplicate-safe confirmation retries.

`make compose-smoke` needs Docker only. It creates a unique project with temporary
synthetic configuration, random loopback ports, and fresh volumes. It builds the
runtime image, checks initial migrations and HTTP readiness, creates a synthetic
owner, collaborator, case, membership, person, vehicle, and evidence of all ten types,
including a retained vehicle-location import and its source and call transcripts/audio, stops and
restarts the stack, and verifies records and original file bytes persist. It also
verifies worker execution through the diagnostic, queues a probe with the worker
stopped, restarts the database/web while keeping the worker stopped, checks the
probe is still queued, and starts the worker to verify completion exactly once
in this controlled run. This does not claim exactly-once delivery after crashes.
It also checks that the production image serves its collected application assets with
debug disabled. Its exit trap removes only that project's containers and volumes. It never uses your `.env`
or data.
The smoke script clears inherited `PANDECT_` settings before applying its synthetic
configuration, including private-runtime endpoints, credentials and upload limits.

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
`python manage.py test --settings=config.test_settings config.tests.browser config.marketing_browser analysis.browser analysis.context_browser cases.map_browser --noinput`; it is
separate from the ordinary
unit suite. Browser fixtures use synthetic accounts/material and temporary
private file storage. Never run competing Django test processes against the same
test database, or point the suite at an investigation database.

`PANDECT_BROWSER_ENGINES` selects `chromium`, `webkit`, or both (comma-separated;
default both). `PANDECT_BROWSER_ARTIFACTS` overrides the screenshot destination,
which defaults to ignored `data/validation/browser`. Browser artifacts are local
validation output. Do not replace the synthetic fixtures with real investigation
material.

The public [marketing-page checks](marketing.md#validation) run alongside the core
suite, with their own captures in `data/validation/marketing-browser`. They cover
navigation and FAQ disclosures without JavaScript, responsive reflow, enlarged
text, keyboard focus, and appearance preferences.

The suite exercises account/case/people/vehicle/evidence/collaboration workflows,
JavaScript uploads and recovery, no-JavaScript file creation/download, keyboard
navigation, light/dark/reduced-motion/forced-colors behavior, responsive reflow,
evidence admin creation/editing, superuser deletion with keyboard/no-JavaScript
confirmation and cancellation, in-app superuser deletion from Details with
confirmation, cancellation, imported-source inventories and no-JavaScript
submission, archive controls and authorized downloads,
location/person-location forms and private JSON downloads, communication forms,
later call attachments, private audio/record downloads, and read-only viewer access, vehicle-location CSV
upload/review/confirmation and source JSON, source fields in file
batches, bulk mixed-file uploads with shared-field inheritance/overrides/reset,
append limits, invalid-file removal, pause/retry, access removal between files,
safe interrupted-confirmation retry, duplicate review across categories and within
a batch, keyboard confirmation, and dense desktop record visibility.
The page-layout journey also captures cases, overview, settings, people, vehicles,
details, and account settings at all five design viewport sizes. The disclosure journey
checks collapsed evidence filters, retained filter URLs, keyboard/no-JavaScript
access to optional descriptions and source fields, and automatic opening with preserved values
after a server validation error. It also opens analysis from the evidence reader
and confirms notes remain accessible in record details. CSV original captures show the compact location
and keyboard access to its retained receipt/source interpretation. It checks page
reflow and uses the evidence-type chooser without JavaScript to open text,
location, and call forms.
Vehicle journeys also capture forms, lists, and details at desktop/mobile sizes,
check keyboard focus and conflict review, and exercise creation, read-only viewer
access, and confirmed owner deletion without JavaScript.
CSV journeys capture desktop, tablet, mobile, 320px reflow, dark, and forced-colors
layouts in both engines; they exercise native disclosure and confirmation controls
by keyboard, the America/Toronto default and upload vehicle selection, queued
progress and explicit refresh, read-only viewer access, and
private JSON downloads without JavaScript.
Duplicate journeys exercise both skipping and keeping matching rows, keyboard
submission with the keep checkbox unchecked, and imported/skipped result counts.
Bulk captures include desktop, tablet, mobile, 320px reflow, enlarged text, dark,
reduced-motion, and forced-colors layouts. Visual targets are 12 complete ordinary
rows at 1440×900 and 14 at 1440×1024, retaining readable text and accessible hit
areas. Manual screen-reader testing is optional during development and does not
block delivery; operator deployment checks remain separate validation activities.
A successful build or HTTP readiness response is not a
substitute for these browser interactions and captures.

## Person POI validation

POI coverage in `make check` exercises Home/Work/custom creation, required labels,
finite/ranged coordinates and database constraints, creator attribution, same-case
person choices, current owner/editor/viewer roles, unrelated superusers, revoked
access, CSRF, stale edits, confirmed deletion and person/case cascades. Map tests
cover private HTML/JSON, escaped labels, bounded independent POI pagination,
shared search, polar positions, and changed asynchronous snapshots.

`cases.map_browser` (included in `make browser-check`) exercises pointer-prefilled
creation, keyboard selection and saving, validation recovery, custom-label edits,
and no-JavaScript person-linked creation/deletion in Chromium and WebKit.
Synthetic captures in `data/validation/maps` include POI forms and maps at the
five design viewports, mobile landscape, enlarged text, dark appearance,
reduced motion, and Chromium forced colors. Tile requests are mocked. These
checks do not establish physical location accuracy or full accessibility compliance.

## Image preview validation

Image evidence preview checks cover current case roles and revoked access,
private headers, unchanged original downloads, orientation, resizing, transparency,
malformed/active content, byte/pixel bounds, and safe failures. Run the focused
checks with:

```sh
uv run --locked --env-file .env python manage.py test --settings=config.test_settings evidence.tests.test_previews --noinput
uv run --locked --env-file .env python manage.py test --settings=config.test_settings config.tests.browser.CoreInterfaceBrowserTests.test_image_evidence_preview_and_download --noinput
```

The preview browser journey checks Chromium and WebKit with and without page
JavaScript, keyboard downloads, all five design viewport sizes, dark appearance,
reduced motion, and forced colors where supported. Captures use the core browser
artifact directory.

## Analysis validation

Run the ordinary checks for retained-content search, current-field reports,
processing/review history, case-scoped inference, native imports, text/style,
conversation, and location calculations:

```sh
uv run --locked --env-file .env make check
uv run --locked --env-file .env python manage.py test --settings=config.test_settings analysis.browser --noinput
uv run --locked --env-file .env python manage.py test --settings=config.test_settings analysis.context_browser --noinput
make compose-smoke
make security-check
```

The focused analysis browser suite is also included in `make browser-check` and
uses Chromium and WebKit with synthetic
material, no JavaScript, all five design viewport sizes, keyboard focus,
light/dark/forced colors, extraction, reviews, downloads, and access revocation.
Captures default to ignored `data/validation/analysis-browser`. When running a
focused suite, do not run another PostgreSQL test process at the same time;
`PANDECT_BROWSER_ENGINES` and
`PANDECT_BROWSER_ARTIFACTS` have the same meaning as in the core suite.

The context browser suite, also included in `make browser-check`, exercises local printed-text profile
selection, searchable OCR regions/private previews and original
downloads, plus historical geographic context, exact retained reference JSON,
viewer access and revocation. It uses real local Tesseract OCR and mocked map
responses with real bounded parsing and processing. Chromium/WebKit captures cover the five design
viewports, keyboard controls, no-JavaScript operation, dark and forced colours,
and reduced motion; output defaults to ignored `data/validation/context-browser`.
These checks do not establish general OCR accuracy or map-service coverage.

Processing tests include real same-database worker dispatch and concurrent
idempotent submissions. Inference/geocoding tests mock HTTP; parser tests decode
synthetic files in real bounded child processes, including real local printed OCR.
Host validation requires Tesseract and English data; see
[development](development.md#run-python-on-the-host). These establish handling and
provenance, not general recognition accuracy. The isolated Compose smoke
also queues native extraction and local printed-image OCR while its worker is stopped, restarts the stack,
then verifies retained extraction/search output after worker recovery.

Regression coverage includes bounded extraction-search queries, shared source
validation, stale and misattributed passages, operation-specific form settings,
review correction and finding navigation, HTML source offsets, and signed-zero
checksum stability after PostgreSQL reload. Admin and case-summary tests reject
inconsistent related-case associations while deletion counts describe all retained
records that will be removed.

## Local inference validation

Local inference boundary coverage is included in `make check`: routing and
immutable execution snapshots, pinned-runtime identity, context/output budgets,
authorization, cancellation, safe HTTP failures, and private native-runtime
authentication/limits. The runtime tests use synthetic model bytes and temporary
loopback listeners, including real process-group termination. They do not load
Whisper or Qwen weights. Run their focused checks independently with:

```sh
uv run --locked python -m unittest inference.test_server
uv run --locked --env-file .env python manage.py test --settings=config.test_settings analysis.tests.test_local_inference config.tests.test_settings --noinput
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
- After those checks pass for a `main` push, CI publishes the Linux amd64 web and
  worker image targets to GHCR. CD resolves their successful-run/commit tags to
  matching digests and deploys the native Dokploy Applications, verifying worker
  startup before the web release. See [continuous deployment](operations.md#continuous-deployment).
- **Scheduled security audit** reruns the dependency audit weekly and on demand.
- **CodeQL** analyzes Python on pushes, pull requests, and a weekly schedule. The
  existing public-repository guard remains: private repositories skip this job
  unless the project deliberately configures an eligible code-scanning plan.

Workflows use pinned action commits and read-only contents permissions. Only CI's
production image jobs receive package-write permission; CD receives package-read
permission and the production environment's deployment secrets. These workflows
do not perform container or secret scanning.

The deployment helper has mocked API and failure-path tests that run without
credentials, network access, or a database:

```sh
uv run --locked python -m unittest config.tests.test_dokploy_deploy
```

They cover stale CI skips, immutable image validation, worker-before-web ordering,
native provider updates, active/failed deployments, sanitized transport failures,
exact running task digests, release markers, and readiness stability. They do not
establish that GitHub secrets, GHCR access, or Dokploy resources are configured.

Local checks do not prove hosted workflow execution or deployed behavior. Hosted
checks run only after changes reach GitHub.

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
