# Application foundation

Pandect has one Django configuration and user model for self-hosted and
operator-hosted operation. Environment variables supply deployment differences.
The [documentation index](README.md) routes to implemented domain workflows.
The UI uses Django templates, local CSS, and focused
vanilla JavaScript for the sequential upload queue and interface enhancements.

## Runtime decisions

Exact runtime versions belong to [`.python-version`](../.python-version),
[`mise.toml`](../mise.toml), [`pyproject.toml`](../pyproject.toml), and the resolved
[`uv.lock`](../uv.lock); container pins belong to the [Dockerfile](../Dockerfile)
and [Compose configuration](../compose.yaml). The project stays on Python 3.14
until a separate upgrade validates the full dependency stack and both CI
architectures. uv manages a non-packaged project with a committed cross-platform
lockfile; commands run from the checkout. Development and integration tests use
PostgreSQL, with a separate test database through Django's built-in test runner.

Sources: [Django downloads](https://www.djangoproject.com/download/),
[Django's Python compatibility table](https://docs.djangoproject.com/en/6.1/faq/install/#what-python-version-can-i-use-with-django),
[Python releases](https://www.python.org/downloads/),
[uv project management](https://docs.astral.sh/uv/guides/projects/).

When upgrading Python, update `.python-version`, `requires-python`, `mise.toml`,
and the Docker base together, regenerate `uv.lock`, and validate both CI
architectures. uv's required version, mise version, Docker stage, and workflow
version must also agree. Docker disables Python downloads: an incompatible base
fails immediately rather than installing a second interpreter into the image.

Headless OpenCV supplies the bounded geometric plate-region detector; it uses the
existing NumPy dependency and adds no GUI, trained model or network service. Its
native processing runs only in the isolated file parser, with one OpenCV thread
and OpenCL disabled. See [plate extraction](analysis-extraction.md#vehicle-plate-candidates).

The Docker image installs locked Python dependencies plus distribution-packaged
Tesseract and English OCR data, runs as an
unprivileged user, and defaults to `gunicorn config.wsgi:application`. Its build
context excludes local secrets and data. App-local templates/static assets are
included explicitly; `collectstatic` runs during the image build and WhiteNoise
serves public application assets. Private evidence is outside its static root.
One application image supplies both web and worker services. Web uses the image's
default Gunicorn command; the production worker overrides it with
`sh /app/scripts/docker-entrypoint-worker.sh`. In native Dokploy deployment, the
singleton worker applies migrations and emits its release
preparation marker before starting queue processing; web activation follows
worker preparation. `APP_COMMIT_SHA` is baked into the shared image and its
OCI revision label. Healthy `/health/` responses report it as `commit`, allowing
deployment checks to distinguish the running release from a healthy older image.
Compose overrides the command for local development. PostgreSQL 18 stores its named volume at `/var/lib/postgresql`, as
required by that image's version-specific layout.

Django's native Tasks API uses `django-tasks-db` with the existing PostgreSQL
database and one `default` queue. Compose runs a separate worker from the same
application image after web readiness confirms migrations. This keeps self-hosting
to PostgreSQL plus application processes, with no separate broker. The dependency
is beta software; integration and restart behavior are covered locally and in CI.
The queue supplies transport and operational records; [case analysis](analysis.md)
adds domain-level authorization, immutable results, progress, and recovery. See
[background tasks](background-tasks.md) for operator diagnostics.

## Modules and identity

- `config/` contains settings, routes, readiness, and WSGI/ASGI entry points.
- `accounts/` contains an `AbstractUser` subclass and its initial migration.
  `AUTH_USER_MODEL = "accounts.User"` is configured before any initial migration.
- django-allauth supplies username/email sign-in, registration, mandatory email
  verification, password recovery, and sign-out under `/accounts/`.
- Django admin uses the custom user model and requires staff permissions.
- `cases/` contains `Case`, the root record for investigation and evidence
  relationships, and `CaseMembership` for viewer/editor collaboration. Ownership
  is separate from membership. Case metadata and the UUID-keyed `CaseInvitation`
  model support the product workflows. `cases.permissions` provides current case
  authorization shared by product views. See [case workflows and permissions](cases.md).
- `/health/` checks database connectivity without returning secrets or case data.
- `core/` is one Django app owning abstract evidence/file bases, common evidence
  management and the unified workspace, plus durable analysis runs, results,
  reviews, artifacts, source resolution, inference adapters, and job orchestration.
  `core.evidence` and `core.analysis` are focused Python packages, not Django apps.
  Core also owns the synthetic worker probe, the bounded `check_task_worker`
  diagnostic, and the safeguard disabling global queue administration because
  operational task records have no case authorization.
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

The pre-release domain reorganization replaces Pandect's migration history with a
fresh baseline. Shared processing models use the `core` app label; concrete
models use their owning domain labels. Old `evidence`, `analysis`, and `vehicles`
app labels, table names, permissions, imports, and queued task paths are not
supported. `accounts.User` remains in its initial migration; upstream Django,
allauth, and queue migrations are unchanged.

Use a new PostgreSQL database and empty queue for this baseline. Do not apply it
on top of an old installation's migration history or use `--fake` to bridge the
schemas. No data conversion is provided. Retain existing databases and media
until an operator deliberately authorizes their replacement or removal; startup
never resets them. Validate with an isolated database first. Web and worker
processes must use the same revision and the new database.

### Dependency boundaries

Domain models inherit from `core.models` and reference `cases.Case`. Core model,
form, storage, and parser contracts do not import domain registries. Explicit
registries compose domain models/forms/readers and processing callables at the
application layer; there is no plugin autodiscovery. Shared file cleanup runs
only after transaction commit, with receivers registered by the owning apps.

The core parser runner retains isolated Python subprocesses, resource limits,
bounded input/output, and a fixed dispatch allowlist. Domain parser modules are
Django-free; they never load settings or models in a parser child. Type-specific
processing remains in the matching domain app, including text methods reused
for extracted document or conversation text.

Each domain's `analysis/` package separates deterministic calculations, metadata
inspection, model-based AI, and external service lookups into `deterministic`,
`metadata`, `ai`, and `external` modules or subpackages as applicable. Local OCR
uses `image.analysis.ai_parser` inside the bounded parser process. Private geocoding
belongs to External while retaining its private processing route. Category names
describe code ownership, not network destination or approval requirements.
Existing mixed operations use domain `workflows.py` modules to combine categories;
their investigator-facing choices and parameters remain unchanged. Shared report
dispatch stays in core, with call calculations in phone and vehicle-location
calculations in geo.

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

The app split preserves browser paths and evidence-kind identifiers. Shared
routes use `core:evidence:*` and `core:analysis:*`; specialized routes belong to
`geo`, `phone`, `socialmedia`, and `vehicle`. Case/people/evidence edits continue
to detect stale revisions and preserve drafts for review.

Application assets use WhiteNoise; originals remain in private Django storage.
Account, case, and invitation responses disable caching and suppress cross-origin
referrers while preserving native same-origin CSRF validation. Routine
access logging redacts invitation, email confirmation, and password reset keys
and query strings. Deployment proxies
need matching treatment. See [operations](operations.md).

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
