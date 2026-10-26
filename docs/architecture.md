# Application foundation

Pandect has one Django configuration and user model for self-hosted and
operator-hosted operation. Environment variables supply deployment differences.
The implemented scope includes account screens, case and people management,
email invitations and viewer/editor collaboration, evidence upload/capture,
private downloads, archive/restore, scoped operator administration, and reproducible
development and validation. The UI uses Django templates, local CSS, and focused
vanilla JavaScript for the sequential upload queue and interface enhancements.

## Runtime decisions

Verified on 2026-10-25:

- Django **6.1.2** is the latest published stable Django release.
- Python **3.15.1** is the latest Python release, but Django's compatibility table
  adds Python 3.15 support in **6.1.3**, which is not published yet.
- Python **3.14.9** is therefore the newest supported stable patch for this
  foundation. `requires-python` deliberately excludes 3.15 until the dependency
  stack supports it. Prereleases are not used.
- uv **0.13.5** resolves a committed, cross-platform `uv.lock`. The application
  is a non-packaged uv project; commands run from the checkout.
- PostgreSQL **18.6** is the development and CI database. Tests use a separate
  PostgreSQL test database through Django's built-in test runner.

Sources: [Django downloads](https://www.djangoproject.com/download/),
[Django's Python compatibility table](https://docs.djangoproject.com/en/6.1/faq/install/#what-python-version-can-i-use-with-django),
[Python releases](https://www.python.org/downloads/),
[uv project management](https://docs.astral.sh/uv/guides/projects/).

When upgrading Python, update `.python-version`, `requires-python`, `mise.toml`,
and the Docker base together, regenerate `uv.lock`, and validate both CI
architectures. uv's required version, mise version, Docker stage, and workflow
version must also agree. Docker disables Python downloads: an incompatible base
fails immediately rather than installing a second interpreter into the image.

The Docker image installs only locked runtime dependencies, runs as an
unprivileged user, and defaults to `gunicorn config.wsgi:application`. Its build
context excludes local secrets and data. Shared templates/static assets are
included explicitly; `collectstatic` runs during the image build and WhiteNoise
serves public application assets. Private evidence is outside its static root.
Compose overrides the command for local development. PostgreSQL 18 stores its named volume at `/var/lib/postgresql`, as
required by that image's version-specific layout.

## Modules and identity

- `config/` contains settings, routes, readiness, and WSGI/ASGI entry points.
- `accounts/` contains an `AbstractUser` subclass and its initial migration.
  `AUTH_USER_MODEL = "accounts.User"` is configured before any initial migration.
- django-allauth supplies username/email sign-in, registration, mandatory email
  verification, password recovery, and sign-out under `/accounts/`.
- Django admin uses the custom user model and requires staff permissions.
- `cases/` contains `Case`, the root record for future investigation and evidence
  relationships, and `CaseMembership` for viewer/editor collaboration. Ownership
  is separate from membership. Case metadata and the UUID-keyed `CaseInvitation`
  model support the product workflows. `cases.permissions` provides current case
  authorization shared by product views. See [case workflows and permissions](cases.md).
- `/health/` checks database connectivity without returning secrets or case data.
- `evidence/` contains abstract `Evidence` and `FileEvidence` bases and concrete
  image, audio, document, and text models. Each record has one required case and
  uploader. Original files/text have generated checksums and are protected against
  replacement through normal model saves. Archive state retains originals; immutable
  submission UUIDs make uncertain upload retries idempotent. See [evidence](evidence.md).
- `people/` contains `Person`, an investigator-maintained name/contact record
  linked to one case, with scoped Django administration. Owners and editors can
  create and edit people; only owners can delete them. See [people](people.md).

Use `get_user_model()` or `settings.AUTH_USER_MODEL` in future applications.
Allauth does not implement case invitations or authorization.

## Primary keys and the initial schema

Every Pandect model uses an explicit, non-editable `UUIDField` primary key with
`default=uuid4`: `User`, `Case`, `CaseMembership`, `CaseInvitation`, `Person`, and
all four concrete evidence models. Each new instance receives an independent UUIDv4 before saving;
use the model's adding state rather than the presence of a primary key to tell
whether it has been saved. Foreign keys to these models use PostgreSQL's native
`uuid` type, including user references in Django admin and allauth.

Declare the same field on future Pandect models. Django's `DEFAULT_AUTO_FIELD`
requires an `AutoField` subclass and cannot be set to `UUIDField`; the setting
remains `BigAutoField` for upstream Django/allauth models and automatically
generated join-table primary keys. Their user foreign keys still use UUIDs.
UUIDs do not replace case authorization. UUIDv4 values do not encode creation
order; use timestamps when ordering by age.

This pre-release change rewrites the four application initial migrations because
there are no active installations or real investigation databases. New databases
start directly with UUIDs; there is no integer-to-UUID data migration. An existing
disposable development database that applied the old integer migrations needs a
fresh database/volume: `migrate` cannot update migrations already marked as applied.
Create a fresh isolated development environment deliberately; no command in the
application automatically deletes or resets an existing database or evidence.

Reference: [Django UUID fields](https://docs.djangoproject.com/en/6.1/ref/models/fields/#uuidfield).

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

The interface change adds `cases/0002_case_interfaces` and the evidence `0002`
migration; it does not rewrite initial migrations or reset a database. Existing
case names are backfilled as `Case #<UUID>`, with update times initialized from
creation times. Existing evidence starts active with a null submission ID.
Case/people/evidence edits detect stale revisions and preserve drafts for explicit
review before saving newer values.

Application assets use WhiteNoise; originals remain in private Django storage.
Account, case, and invitation responses disable caching and suppress cross-origin
referrers while preserving native same-origin CSRF validation. Routine
access logging redacts invitation tokens and query strings; deployment proxies
need matching treatment. See [operations](operations.md).

## Planned work

Extraction, document/media previews, background processing, AI, cross-case/global
search, graphs, maps, timelines, and reporting remain planned. No queue, external
AI client, or paid service is required by core management. Future inference must
use the centralized Cloudflare AI Gateway boundary from [AGENTS.md](../AGENTS.md).
Ownership transfer and self-service account deletion are not implemented.
There is deliberately no public evidence/media route.
