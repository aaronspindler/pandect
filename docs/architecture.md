# Application foundation

Self-hosted and operator-hosted Pandect share one Django configuration and user
model, with deployment differences supplied by environment variables. The UI uses
Django templates, local CSS, and focused vanilla JavaScript. See the
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

The unprivileged Docker image installs locked Python dependencies, Tesseract, and
English OCR data. Its build context excludes local secrets/data and explicitly
includes app-local templates/assets. Build-time `collectstatic` supplies public
assets to WhiteNoise; private evidence stays outside the static root.

Web and worker share one image. Production web defaults to
`gunicorn config.wsgi:application`; the worker uses
`sh /app/scripts/docker-entrypoint-worker.sh`. In Dokploy, the singleton worker
migrates and emits its release preparation marker before processing tasks; web
activation follows. `APP_COMMIT_SHA` is baked into the image and OCI revision label
and returned as `commit` by healthy `/health/` responses. Compose overrides startup
commands for development. PostgreSQL 18's named volume mounts at `/var/lib/postgresql`.

Django's Tasks API uses the beta `django-tasks-db` dependency, PostgreSQL, and one
`default` queue, without a separate broker. Compose starts its worker after web
readiness confirms migrations. Integration/restart behavior is covered locally
and in CI. The queue supplies transport and operational records;
[case analysis](analysis.md) adds authorization, immutable results, progress, and
recovery. See [background tasks](background-tasks.md) for diagnostics.

## Modules and identity

- `config/` contains settings, routes, readiness, and WSGI/ASGI entry points.
- `accounts/` owns the `AbstractUser` subclass and initial migration;
  `AUTH_USER_MODEL = "accounts.User"` is set before any initial migration.
- django-allauth supplies username/email sign-in, registration, mandatory email
  verification, password recovery, and sign-out under `/accounts/`.
- Django admin uses the custom user model and requires staff permissions.
- `cases/` owns the investigation boundary (`Case`), viewer/editor collaboration
  (`CaseMembership`), and UUID-keyed `CaseInvitation`. Ownership is separate from
  membership. `cases.permissions` supplies current case authorization to product
  views. See [case workflows and permissions](cases.md).
- `/health/` checks database connectivity without returning secrets or case data.
- `core/` is one Django app owning abstract evidence/file bases, common evidence
  management and the unified workspace, plus durable analysis runs, results,
  reviews, artifacts, source resolution, inference adapters, and job orchestration.
  `core.evidence` and `core.analysis` are focused Python packages, not Django apps.
  Core also owns the synthetic probe, bounded `check_task_worker` diagnostic, and
  disabled global queue admin: operational task records have no case authorization.
- `text/`, `image/`, `audio/`, and `document/` own their concrete evidence models,
  forms, reader templates, administration, processing, and tests.
- `geo/` owns location/person-location/vehicle-location evidence, points of
  interest, maps, geocoding, geographic analysis, and vehicle-location CSV imports.
- `phone/` owns message and call evidence, retained call additions, imported
  communication context, message imports, and conversation analysis.
- `people/` and `vehicle/` own case-scoped person and vehicle records. These are
  investigator-maintained entities, not evidence subclasses. `socialmedia/` owns
  social accounts linked to people. `video/` is a registered placeholder only.
- `marketing/` owns public routes, templates, styles, and tests. It shares small
  design primitives and brand assets with `ui/`, which owns the product shell,
  shared controls, and error templates. Every template and static asset lives in
  its owning app; template `DIRS` and `STATICFILES_DIRS` are empty.

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

The core parser runner uses isolated Python subprocesses, resource limits,
bounded input/output, and a fixed dispatch allowlist. Domain parsers are Django-free
and never load settings/models in a child process. Type-specific processing stays
in its domain, including text methods reused for document or conversation text.

Domain `analysis/` packages separate `deterministic`, `metadata`, `ai`, and
`external` processing. Local OCR uses `image.analysis.ai_parser` in the bounded
parser. Private geocoding belongs to External but retains its private route:
categories describe code ownership, not destination or approval. Domain
`workflows.py` modules combine categories without changing investigator choices
or parameters. Core dispatches reports; phone owns call calculations and geo
owns vehicle-location calculations.

## Interface delivery and migrations

The shared shell follows [DESIGN.md](../DESIGN.md). At wide desktop sizes,
evidence uses case navigation, a dense record list, and a reading pane. Smaller
screens use the same canonical list/detail URLs and full-page form workflows.
JavaScript enhances batching; core record management works without it.

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
[vehicle-location imports](vehicle-location-imports.md). Optional private
text/transcription services have separate resource bounds, pinned native engines,
and operator-provisioned models on an internal network; setup and qualification
belong to [local analysis](local-analysis.md). Core management and deterministic
analysis do not require an external AI service.

The [roadmap](evidence-analysis-roadmap.md) owns future analysis priorities.
Ownership transfer and self-service account deletion remain unimplemented.
There is no public evidence/media route.
