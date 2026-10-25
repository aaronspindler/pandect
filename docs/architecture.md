# Application foundation

Pandect has one Django configuration and user model for self-hosted and
operator-hosted operation. Environment variables supply deployment differences.
The implemented scope is authentication, case and collaboration models, scoped
case administration, person records and administration, evidence storage models,
database readiness, dependency management, and reproducible development and
validation.

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
context excludes local secrets and data. Compose overrides the command for local
development. PostgreSQL 18 stores its named volume at `/var/lib/postgresql`, as
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
  is separate from membership. See [case models and admin permissions](cases.md).
- `/health/` checks database connectivity without returning secrets or case data.
- `evidence/` contains abstract `Evidence` and `FileEvidence` bases and concrete
  image, audio, document, and text models. Each record has one required case and
  uploader. Original files/text have generated checksums and are protected against
  replacement through normal model saves. See [evidence storage](evidence.md).
- `people/` contains `Person`, an investigator-maintained name/contact record
  linked to one case, with scoped Django administration. Owners and editors can
  create and edit people; only owners can delete them. See [people](people.md).

Use `get_user_model()` or `settings.AUTH_USER_MODEL` in future applications.
Allauth does not implement case invitations or authorization.

## Planned work

Case invitations, investigator-facing screens, evidence upload/download workflows
and processing, background jobs, search, visual analysis, and reporting are not
implemented. No queue, external AI client, or paid service is required by the
foundation. Future inference must use the centralized Cloudflare AI Gateway
boundary from [AGENTS.md](../AGENTS.md).
Evidence must remain private: there is deliberately no public media URL route.
Production deployment automation, evidence authorization workflows, and a polished
product interface will be delivered with those capabilities.
