# Application foundation

Self-hosted and operator-hosted Pandect share one Django configuration and user
model, with deployment differences supplied by environment variables. The UI uses
Django templates, local CSS, locally served htmx, and focused JavaScript. See the
[documentation index](README.md) for domain workflows.

## Runtime decisions

Exact runtime versions belong to [`.python-version`](../.python-version),
[`mise.toml`](../mise.toml), [`pyproject.toml`](../pyproject.toml), and the resolved
[`uv.lock`](../uv.lock); container pins belong to the [Dockerfile](../Dockerfile)
and [Compose configuration](../compose.yaml). Stay on Python 3.14 until an upgrade
validates the full dependency stack on both CI architectures. uv manages this
non-packaged project with a committed cross-platform lockfile; run commands from
the checkout. Development and integration tests use PostgreSQL; Django's test
runner creates a separate test database.

Sources: [Django downloads](https://www.djangoproject.com/download/),
[Django's Python compatibility table](https://docs.djangoproject.com/en/6.1/faq/install/#what-python-version-can-i-use-with-django),
[Python releases](https://www.python.org/downloads/),
[uv project management](https://docs.astral.sh/uv/guides/projects/).

When upgrading Python, update `.python-version`, `requires-python`, `mise.toml`,
and the Docker base together, regenerate `uv.lock`, and validate both CI
architectures. uv's required version, mise version, Docker stage, and workflow
version must also agree. Docker disables Python downloads: an incompatible base
fails immediately rather than installing a second interpreter into the image.

Headless OpenCV provides bounded geometric plate detection using NumPy, without a
GUI, trained model, or network service. It runs only in the isolated file parser,
with one thread and OpenCL disabled. See
[plate extraction](analysis-extraction.md#vehicle-plate-candidates).

The unprivileged Docker image installs the locked Python dependencies. Its build context excludes local secrets/data and explicitly
includes app-local templates/assets. Build-time `collectstatic` supplies public
assets to WhiteNoise; private evidence stays outside the static root.

Web and the background worker share one image. Production web runs Gunicorn with
`config.asgi:application` and the native ASGI worker. The web process handles
Django requests and serves the case event stream on the same origin. Its ASGI
lifespan owns one PostgreSQL listener per process. In Dokploy, the singleton
background worker runs `sh /app/scripts/docker-entrypoint-worker.sh`, migrates
and emits its release preparation marker before web activation. `APP_VERSION` carries the published `vN.N` identifier in the
image, OCI version label, and healthy `/health/` response's `version` field;
local builds default to `dev`. `APP_COMMIT_SHA` retains the exact source revision
in the image, OCI revision label, and health response's `commit` field. See
[release numbering and deployment](operations.md#continuous-deployment).
Compose overrides startup commands for development. PostgreSQL 18's named volume
mounts at `/var/lib/postgresql`. Compose and CI pin the same multi-architecture
official PostgreSQL image. [Cloudflare AI Search](cloudflare-search.md) indexes
eligible active passages in one private instance per enabled case. Django verifies
each hit against its source and keeps the canonical findings and answer history.

Django's Tasks API uses the beta `django-tasks-db` dependency and PostgreSQL
without a separate broker. Ordinary workers use the `default` queue. The optional
[offline Instagram pilot](instagram-monitors.md) has its own worker and
`instagram` queue in the same database. Compose starts workers after web readiness
confirms migrations. Integration and restart behavior are covered by local and
CI checks. The queue supplies transport and operational records.
[Case analysis](analysis.md) adds authorization, immutable results, progress, and
recovery. See [background tasks](background-tasks.md) for diagnostics.

## Modules and identity

- `config/` contains settings, routes, readiness, and WSGI/ASGI entry points.
- `accounts/` owns the `AbstractUser` subclass and initial migration;
  `AUTH_USER_MODEL = "accounts.User"` is set before any initial migration.
- django-allauth supplies username/email sign-in, registration, mandatory email
  verification, password recovery, and sign-out under `/accounts/`.
  `accounts.verification` checks the saved verification state for product
  requests, upload and event gates, and background jobs.
- Django admin uses the same allauth sign-in flow and requires staff permissions.
  Authentication rate limits share Django's PostgreSQL cache. An accounts
  migration creates the upstream cache schema; it is not a Pandect domain model.
- `request_activity/` owns optional [origin request telemetry](request-activity.md),
  including a private delivery buffer, a separate PostgreSQL database, and a
  read-only view for authorized site operators.
- `cases/` owns the investigation boundary (`Case`), viewer/editor collaboration
  (`CaseMembership`), and UUID-keyed `CaseInvitation`. Ownership is separate from
  membership. `cases.permissions` supplies current case authorization to product
  views and can reload and verify an actor for background work. See
  [case workflows and permissions](cases.md).
- `/health/` checks database connectivity without returning secrets or case data.
- `core/` is one Django app owning abstract evidence/file bases, common evidence
  management and the unified workspace, plus durable analysis runs, results,
  address estimate selections, artifacts, source resolution, inference adapters, and job orchestration.
  `core.evidence` and `core.analysis` are focused Python packages, not Django apps.
  The removed Selections tool has no routes or UI. Its `SavedSelection` model/table
  remain to preserve stored references and case deletion; no data migration or
  database cleanup accompanies the removal.
  `core.evidence_chat` owns case chat retrieval, citation validation, and
  checkpointed reviews. Additive migration `core.0017_evidence_chat` stores
  conversations, source versions, coverage, and cited matches. Apply it before
  starting the matching worker and web versions. Chat replies can be marked
  Important, and external gateway usage links to the exact turn.
  Migrations `core.0018` through `core.0022` add durable file cleanup, short-lived
  deletion reviews, and concurrent dependency indexes. Migrations `core.0028`
  through `core.0030` index saved chat activity, ordered processing history,
  and exact analysis-batch scope lookups. `core.0031_chat_turn_activity` adds
  bounded live milestones to saved turns. `core.0032_chat_turn_model_usage`
  records cumulative model reservations on each ordinary turn.
  `core.0033_chat_routing_approval` stores the case owner's separate approval
  for OpenAI source selection. `core.0034_approved_chat_extraction` links the
  proposed PDF extraction, its approval, the analysis run and the follow-up
  answer. It also records submission IDs for reused analyses. Apply these
  migrations before starting the matching web and worker versions.
  The `phone` and `geo` chronology
  migrations add partial indexes for active communications and location evidence
  by case and event time. Each new index builds concurrently in a separate
  migration. If a build is interrupted, check the named index for invalid state
  before retrying the migration.
  Core also owns the synthetic probe, bounded `check_task_worker` diagnostic, and
  disabled global queue admin: operational task records have no case authorization.
- `text/`, `image/`, `audio/`, and `document/` own their concrete evidence models,
  forms, reader templates, administration, processing, and tests.
- `geo/` owns location/person-location/vehicle-location evidence, points of
  interest, maps, geocoding, geographic analysis, and vehicle-location CSV imports.
- `phone/` owns message and call evidence, retained call additions, imported
  communication context, message and call history imports, and conversation
  analysis. Migration
  `phone.0005_conversation_grouping` adds grouping revisions, sessions and
  ordered membership rows. Apply it before serving the updated reader or worker.
  Migration `phone.0006_conversation_pair_revision` adds change tracking for
  active pair originals. Existing finding-backed sessions keep their source links.
  Migration `phone.0008_call_history_import` adds case source lines, retained
  import rows, and attributed call-person links. Apply it before starting the
  call import web and worker paths. Existing call original bytes are preserved.
  Migration `phone.0012_group_message_destination` adds the group CSV destination
  and limits pair revision triggers and the pair index to direct messages.
  It rebuilds the pair index in the migration transaction, which holds a table
  lock. Existing direct message original bytes are preserved.
- `people/` and `vehicle/` own case-scoped person and vehicle records. These are
  investigator-maintained entities, not evidence subclasses. `socialmedia/` owns
  social accounts linked to people and [case-bound Instagram monitors](instagram-monitors.md). `video/` is a registered placeholder only.
- `marketing/` owns public routes, templates, styles, and tests. It shares small
  design primitives and brand assets with `ui/`, which owns the product shell,
  shared controls, and error templates. Use the shared pagination component for
  standard page links, including independently paginated sections. Every template
  and static asset lives in its owning app; template `DIRS` and
  `STATICFILES_DIRS` are empty.

Use `get_user_model()` or `settings.AUTH_USER_MODEL` in future applications.
Allauth does not implement case invitations or authorization.

## Primary keys and the initial schema

Every concrete Pandect model uses an explicit, non-editable UUID primary key
with `default=uuid4`. Django/allauth internals and automatic join tables retain
upstream schemas. UUIDs do not replace case authorization.

The pre-release reorganization replaces Pandect's migration history with a fresh
baseline. Shared processing models use `core`; concrete models use domain app
labels. Old `evidence`, `analysis`, and `vehicles` labels, tables, permissions,
imports, and queued task paths are unsupported. `accounts.User` remains in its
initial migration; upstream Django, allauth, and queue migrations are unchanged.

Use a new PostgreSQL database and empty queue. Do not apply this baseline over old
migration history or bridge schemas with `--fake`; no data conversion is provided.
Retain existing databases/media until an operator authorizes replacement or
removal; startup never resets them. Validate in isolation first, then use the same
revision and new database for web and worker.

### Dependency boundaries

Domain models inherit from `core.models` and reference `cases.Case`. Core model,
form, storage, and parser contracts do not import domain registries. Explicit
registries compose domain models/forms/readers and processing callables at the
application layer; there is no plugin autodiscovery. Shared file cleanup runs
only after transaction commit, with receivers registered by the owning apps.

### Application services

Keep multi-step domain workflows in focused modules in the owning app. Views,
admin hooks, task functions, and management commands adapt requests, model
forms, queue payloads, or command arguments. They call the workflow and handle
rendering, redirects, and reporting. Services check current authorization when
they own a user-initiated mutation and keep locking, writes, and queue
publication together where the operation requires atomicity. Model constraints
and validation still protect individual records. Simple model form CRUD can
stay with its entry point.

| Workflow | Service owner |
| --- | --- |
| Case invitation issuance, delivery, and acceptance | `cases.invitations` |
| Case External AI approval and hosted index lifecycle | `cases.analysis_settings`, `core.case_search.lifecycle` |
| Product evidence intake, duplicate review, and shared archive transitions | `core.evidence.retention` |
| Important marks and dated notes | `core.important.services` |
| Analysis submission, worker execution, and retry | `core.analysis.services`, `core.analysis.worker`, `core.analysis.retries` |
| Analysis input snapshots, source resolution, and result validation | `core.analysis.source_selection`, `core.analysis.source_resolution`, `core.analysis.result_validation` |
| Sighting review and address estimate selection | `core.analysis.sightings`, `core.analysis.address_estimates` |
| Evidence Chat submission, retry, and recovery | `core.evidence_chat.services` |
| Evidence Chat delivery, saved progress, and exhaustive review | `core.evidence_chat.jobs`, `core.evidence_chat.checkpoints`, `core.evidence_chat.full_review` |
| Vehicle CSV retention, preparation, and publication | `geo.vehicle_import_jobs` |
| Plan comparison source review and submission | `geo.journey_claim_requests` |
| Call additions and conversation summary requests | `phone.call_attachments`, `phone.instance_summaries` |
| Text claim source review and quotation matching | `text.claim_comparison_requests` |

Case routes are owned by the investigation list and lifecycle, settings,
search settings, and collaboration modules in `cases`. Evidence routes
live in `core.evidence.intake_views`, `workspace_views`, and `record_views`.
The analysis URL configuration points to the adapters for submission,
reprocessing, saved runs, reports, and private outputs. Saved finding reader
context lives in `core.analysis.result_reader`. `ui.forms.SignedRevisionForm`
handles revision validation and retry tokens for records edited from a
saved version.

Existing queued task paths stay stable across service refactors. Keep task
entry points importable while persisted deliveries can refer to them.
Submission references identify retries within the initiating account and case.
Analysis batches and vehicle imports retain separate record IDs for links and
queued deliveries. The scoped-submission migrations preserve those IDs and
backfill the existing retry references. Stop older web and worker writers before
applying the migrations and start the matching versions together.
After independent cases reuse a submission reference, the old global uniqueness
constraints can no longer be restored in place. Keep a pre-upgrade backup.

### Search and processing

Cloudflare AI Search owns passage retrieval, matching and indexing. PostgreSQL
stores the per-case private instance identity, versioned sync inventory and
durable cleanup work. Each hit must match the current case, retained source
version and archive state before its text can leave Django. The case owner must
enable External AI for both Case Search and Case Chat.

The investigation Worker uses one Durable Object per investigation to choose and
sequence tools. Django enforces the turn's cutoff, permissions, execution budget
and approved destinations. It opens original passages and publishes the verified
answer. Immutable results and historical selections stay readable after the
search cache is retired. See [search](cloudflare-search.md) and
[the Agent](cloudflare-agent.md).

During ingestion a model can propose source-anchored narrative relationships.
Pandect stores the endpoints, qualification, exact passage and originating result
in PostgreSQL. Reviewing a lead records an attributable decision. Case Chat can
follow an unreviewed lead, and reopens its source before citing it.

`core.analysis.published` owns shared queries for saved analysis and its
extracted passages. `core.evidence.source_clocks` owns how original timestamps
are classified for chronology, search, and chat.

The core parser runner uses isolated Python subprocesses, resource limits,
bounded input/output, and a fixed dispatch allowlist. Domain parsers are Django-free
and never load settings/models in a child process. Type-specific processing stays
in its domain, including text methods reused for document or conversation text.
`core.parsers.read_source` verifies the case storage key, byte count, and
checksum for original previews and parser-backed analysis. `core.csv_import`
shares bounded CSV reading and local timestamp resolution. Import format rules
and error messages live with the phone and vehicle importers.
Audio decoding uses pinned PyAV with bundled FFmpeg libraries inside this same
boundary; no FFmpeg executable or separate service is required. See the
[recording profiles and bounds](original-previews.md#recordings).

Domain `analysis/` packages separate `deterministic`, `metadata`, `ai`, and
`external` processing. The bounded parser prepares metadata-free images or
rendered PDF pages for OCR through the case-approved Cloudflare vision route.
Geographic services keep their independently configured destinations. Domain
`workflows.py` modules combine categories without changing investigator choices
or parameters. Core dispatches reports; phone owns call calculations and geo
owns vehicle-location calculations.

## Interface delivery and migrations

The case shell follows [DESIGN.md](../DESIGN.md). Django owns the canonical
page, permissions, forms, and fragments. Locally served htmx enhances same-case
navigation and paging. Private HTML is fetched again on history navigation.
Case switches and transitions across map tile security policies use document
navigation. Ordinary links and form submissions work without JavaScript.

PostgreSQL maintains case activity revisions inside domain write transactions,
including bulk writes. Each new evidence or processing model registers the
collections it affects in a migration. A transactional notification wakes a
shared PostgreSQL listener in each ASGI web worker. The same-origin
`/events/cases/<uuid>/` endpoint sends an invalidation signal to visible,
authorized case tabs. The browser checks current counts, revisions and case
access when a tab becomes visible. If an earlier check is still running, the
tab requests a fresh snapshot after it finishes. It also reconciles on stream
connection and after changes. Existing authorized HTML fragments use
`X-Pandect-Live: 1` for progress and `X-Pandect-Live: collection` for
collection results. Lists offer an in-place refresh while processing status
updates automatically. The polling and manual refresh paths recover from a
stream outage.

Product entry points require an active account with a verified allauth email.
Case authorization is separate from Django staff/model permissions. Current roles
and parent-case ownership are checked on every relevant request and again inside
case-locked mutations. Invitation acceptance is bound to the invited verified
email and never silently changes an existing membership or transfers ownership.

Browser paths and evidence-kind identifiers are preserved. Shared routes use
`core:evidence:*` and `core:analysis:*`; specialized routes belong to `geo`,
`phone`, `socialmedia`, and `vehicle`. Case/people/evidence edits detect stale
revisions and preserve drafts for review.

Account, case, and invitation responses disable caching and cross-origin
referrers while preserving same-origin CSRF validation. Access logs redact query
strings and invitation, email-confirmation, and password-reset keys; proxies need
matching treatment. See [operations](operations.md).

## Capability boundaries

Implemented analysis and inputs are listed in [analysis](analysis.md) and
[vehicle-location imports](vehicle-location-imports.md). The verified case owner enables hosted search and AI in Case Settings.
[Case Chat](cloudflare-agent.md) uses a Cloudflare Agent and the approved model
route. Django retains deterministic extraction, source verification and saved work.

The [roadmap](evidence-analysis-roadmap.md) owns future analysis priorities.
Ownership transfer and self-service account deletion remain unimplemented.
There is no public evidence/media route.
