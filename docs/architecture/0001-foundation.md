# ADR 0001: Local-first application foundation

- Status: Accepted
- Date: 2026-08-02
- Scope: Foundation only

## Context

Pandect begins as a self-hosted investigation application for one trusted operator on one machine. The first milestone needs a small, dependable platform on which domain capabilities can later be built. It does not need the complexity, operational cost, or privacy implications of a hosted multi-user service.

The absence of application authentication is deliberate for this milestone. Consequently, binding the supported runtime to loopback is a security requirement, not merely a development convenience. Operators must not expose the default stack to an untrusted network.

## Decision

### Application and interface

- Django is the backend, URL router, and page-rendering framework.
- Django templates and semantic HTML are the frontend baseline. Ordinary links and forms remain the default interaction model.
- HTMX is a locally served, pinned progressive-enhancement dependency used only when replacing part of a page has a clear benefit. There is no Node.js toolchain, CDN dependency, client state store, or single-page application.
- The initial `core` Django app contains platform concerns only: the home page, health/readiness checks, and demonstrative task plumbing.
- The product surface does not include Django admin or authentication. No custom user, login, role, permission, ownership, or tenancy model is introduced.

### Local persistence and tasks

- SQLite is the default and supported application database for the single-machine, limited-concurrency operating model.
- The application database and Huey queue are separate SQLite files under one persistent data directory. That directory is configured by `PANDECT_DATA_DIR` and can be backed up as a unit.
- SQLite connections use a documented busy timeout to tolerate brief local contention; this is not a claim of high-concurrency support.
- Huey provides background execution through its Django integration and `SqliteHuey`, with one conservative worker by default. Tasks are eager in automated tests.
- Raw evidence will remain canonical when domain collection is later introduced. Derived representations must be separate, deterministic where practical, and traceable to their source, as required by the root `AGENTS.md`.

### Runtime and command surface

- Docker Compose is the supported local runtime. A web service and a Huey worker share one application image and persistent data mount.
- The host publishes the web port on `127.0.0.1` by default. The container may listen on its private interface so Compose can forward the loopback-bound host port.
- Migrations are explicit; neither long-running service races to apply them at startup.
- Make is the stable, documented command surface for setup, lifecycle, Django management, testing, and maintenance. Direct Compose commands remain available for debugging.
- Project-owned static files are collected and served locally by the Django WSGI application through WhiteNoise. Gunicorn runs the supported container web process.

```text
Browser on this machine
        |
        | http://127.0.0.1:8000
        v
Docker Compose -> Django/Gunicorn -> application.sqlite3
                        |
                        | enqueue task
                        v
                 Huey SQLite queue
                        |
                        v
                  Huey worker
```

### Repository layout

```text
pandect/
  config/        # Django settings, URLs, WSGI
  core/          # platform-only views, checks, tasks, commands
templates/       # full pages and explicit partials
static/          # project-owned CSS and vendored browser assets
tests/           # cross-cutting foundation tests
docs/            # architecture and operating guidance
scripts/         # small setup and validation helpers
data/            # ignored persistent local state
```

Configuration uses a small set of explicit `PANDECT_` environment variables documented in `.env.example`. A local secret is generated into the persistent directory with restrictive permissions unless `PANDECT_SECRET_KEY` is explicitly supplied. Logs describe failures without dumping configuration or secret values.

Runtime dependencies live in `project.dependencies`; development and validation dependencies live in the `dev` dependency group. `uv.lock` is committed, CI installs the locked environment, and updates are made intentionally through `uv` followed by the full validation suite. Python 3.13 and Django 5.2 LTS are selected for their supported lifetime and mature ecosystem; patch releases remain locked by `uv.lock`.

Tests use pytest and pytest-django. Unit and request tests use temporary state, tasks run eagerly where appropriate, and no validation path requires network access or third-party credentials. `make check` is the single complete local validation command and matches CI's core checks.

## Deferred change triggers

These choices should change only in response to demonstrated needs and a separate decision:

- Consider PostgreSQL if measured write concurrency, database size, or required SQL capabilities exceed SQLite's single-machine limits.
- Consider Redis or another queue backend if measured task throughput or multi-host workers require it.
- Add authentication and authorization before any supported untrusted-network or multi-operator use.
- Consider a separate frontend toolchain only when concrete interface requirements cannot be maintained well with server-rendered HTML and targeted HTMX.
- Consider remote infrastructure only when a required capability cannot reasonably run locally, with explicit privacy, security, cost, and failure analysis.

## Consequences

The default installation has few moving parts, retains data locally, and can be operated and backed up as one directory. It intentionally accepts limited concurrency, a trusted-local-machine security boundary, and explicit lifecycle commands. Future scaling or multi-user work requires deliberate architecture and security decisions rather than silent expansion of this foundation.

## Non-goals

- Investigation entities, relationships, cases, evidence graphs, timelines, search, or identity resolution
- Source adapters, connectors, scraping, polling, browser automation, or source credentials
- Login, registration, users, roles, permissions, teams, ownership, or tenancy
- A public API, mobile client, SPA, or generalized component or plugin framework
- PostgreSQL, Redis, Celery, Kubernetes, cloud-managed services, remote telemetry, or internet-facing deployment as defaults
- High-throughput, multi-host, multi-region, or horizontally scaled operation
