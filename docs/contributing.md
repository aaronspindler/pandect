# Contributing to the foundation

Read the relevant issue, the root [`AGENTS.md`](../AGENTS.md), and
[`architecture/0001-foundation.md`](architecture/0001-foundation.md) before
changing Pandect. The root guidance is authoritative for engineering style,
local-first operation, evidence integrity, and validation.

## Validation

Validation runs in the profile-gated Docker `tools` service, using Python and
development dependencies pinned by the project lockfile and image. Host Python
and `uv` are not required; do not substitute an independent dependency
workflow.

Run the focused tests while developing, then leave the repository with the full
validation passing:

```console
make test
make lint
make validate
make check
```

The narrower targets match the separately reported CI steps. `make validate`
runs Django's system check, detects missing migrations, validates the Compose
configuration, and builds the runtime image. `make check` runs all three
targets as the complete local pre-submit check.

Tests must be hermetic: use temporary state and eager Huey execution, with no
third-party credentials or network access. Commit intentional migrations with
the model change that needs them. Keep dependencies minimal and update the
dependency declaration and lockfile together rather than adding a second
toolchain. Dependency updates are the one workflow that currently needs the
same `uv` version pinned in the Dockerfile and used by CI's tools image; after
editing `pyproject.toml`, run `uv lock`, review `uv.lock`, and finish with
`make check`.

## Adding a future Django app

Add an app only for a concrete, cohesive capability that does not belong in the
platform-only `pandect.core` app. Follow the existing `pandect/<app_name>`
package layout and explicit Django wiring rather than introducing a new
framework:

1. Give the app one clear domain responsibility and a narrow public interface.
2. Add it explicitly to Django settings and URL routing only where needed.
3. Namespace its templates and URLs; keep full-page navigation usable without
   JavaScript and use HTMX only for a justified fragment interaction.
4. Keep migrations, tests, tasks, and management commands with the capability
   they implement. Make external side effects visible and testable.
5. Preserve raw evidence as the canonical record. Store derived or normalized
   representations separately with provenance and source timestamps so they
   can be reproduced and traced back.
6. Update operating or extension documentation when the change adds setup,
   configuration, security, or registration steps.
7. Run `make check`.

Do not couple a new app to login, users, roles, ownership, permissions, or
tenancy. The current trusted-operator boundary has no application
authentication. Authentication and support for untrusted access require a
separate architecture and security decision; adding incidental user foreign
keys or login decorators does not provide that boundary.

Do not make application startup depend on cloud services, managed storage,
hosted queues, remote observability, or credentials for a future source. Local
processing and persistence remain the default.

## Templates and HTMX

Full pages extend `templates/base.html`. Put app-specific full templates under
`templates/<app_name>/` and fragments under
`templates/<app_name>/partials/`. Prefer an ordinary full-page response; when a
targeted partial update has a concrete benefit, the same view may select a
fragment for an `HX-Request` and retain a useful non-HTMX fallback.

Keep POST paths protected by Django CSRF handling. The base template supplies
the CSRF header for HTMX, while ordinary forms still include `{% csrf_token %}`.
Serve browser assets from the project-owned `static/` tree. Do not add a CDN,
Node.js pipeline, client state store, or SPA framework for a fragment update.

## Adding a future source

There is intentionally no generic source registry or plugin system yet. The
first real source should establish only the smallest concrete boundary its
requirements demonstrate. Keep its authentication, network client, collection,
normalization, rate-limit handling, retries, and failure reporting isolated so
another source can later be added without editing unrelated integrations.

When a source inherently requires a remote API, make configuration explicit
and avoid contacting it during imports, startup, health checks, or unrelated
workflows. Document why local processing is not feasible; exactly what data
leaves the machine and which service receives it; credential, cost, privacy,
retention, and availability implications; and behavior when the service is
unavailable. Keep the default application usable without those credentials.

Source tasks should have bounded timeouts and be idempotent where practical.
Retry only operations known to be safe. Store raw responses or collected
artifacts without destructive normalization, and link every derived record to
its raw source and provenance. Errors must be actionable without logging
credentials or sensitive collected content.

After repeated source implementations reveal a stable common need, extract a
small shared interface and document its minimum implementation, tests, and
explicit registration steps. Do not predict those requirements by building a
general plugin framework in advance.
