# Local development

Install Docker with Compose v2 and
[uv](https://docs.astral.sh/uv/getting-started/installation/) at the version in
[`mise.toml`](../mise.toml). uv manages Python, `.venv`, and dependencies.
If you use mise, run `mise install` for the pinned tools.

## Pre-release database baseline

Use a fresh database and empty queue when coming from the old pre-release
schema. See the [migration baseline](architecture.md#primary-keys-and-the-initial-schema).

## First run

From the repository root:

```sh
uv sync --locked
make install-hooks
cp .env.example .env
uv run --locked python - <<'PY'
import secrets
from pathlib import Path

path = Path(".env")
content = path.read_text()
for name in ("DJANGO_SECRET_KEY", "POSTGRES_PASSWORD"):
    content = content.replace(f"{name}=\n", f"{name}={secrets.token_urlsafe(50)}\n")
path.write_text(content)
path.chmod(0o600)
PY
make run
```

Use `make run` for subsequent starts and rebuilds, with your usual Compose
profiles. It builds first, then waits for workers' active jobs to finish before
restarting. PostgreSQL retains pending jobs. Run one rebuild at a time. If you
interrupt the wait, let Docker finish stopping the workers before retrying.
Check `/events/health/` for event listener readiness.

Verify queue execution with
`docker compose exec web python manage.py check_task_worker --timeout 30`.
`docker compose stop` retains database and evidence volumes.

Open <http://localhost:8000/> (or `PANDECT_WEB_PORT`), create an account, and verify
its email before opening case material. The local Compose web command applies
pending migrations before starting Gunicorn. With
`DJANGO_DEBUG=true`, private verification and invitation links appear in
`docker compose logs web`. Keep those logs private. Production uses SMTP.

Use debug mode only on a trusted local instance. Its Django Debug Toolbar
displays read-only request diagnostics. Reload to inspect a fresh request after
navigating within a case. Rebuild Compose with `make run` after changing templates
or assets. Host debug development serves source assets directly.

After verification, create a case from **All cases** and use **Add evidence**.
Ordinary investigators do not need staff permissions. See
[case workflows](cases.md), [evidence](evidence.md), and
[vehicle-location imports](vehicle-location-imports.md) for task-specific guidance.

To create an optional `/admin/` operator:

```sh
docker compose exec web python manage.py createsuperuser
```

## Run Python on the host

Keep the database and media together. Host Django defaults to the checkout's
`data/media`, while Compose uses `/app/data/media` on the named `app_data`
volume. Use a separate database and media directory for host development unless
both runtimes use the same persistent storage and keys. When switching back to
Compose, stop the host server and workers first. If a database already references
files in more than one root, include all of them in a
[coordinated backup](operations.md#capture-a-matching-backup).

With the intended database and storage configured, run:

```sh
docker compose stop web
docker compose --profile instagram-pilot stop --timeout -1 worker instagram-worker
docker compose up -d --wait db
uv run --locked --env-file .env python manage.py migrate
uv run --locked --env-file .env python manage.py runserver
```

The Compose web service serves pages and the case event stream on one loopback
port. Host-only `runserver` uses polling for activity updates. Use the Compose
stack to exercise push delivery.

Use `uv run --locked --env-file .env python manage.py <command>` for management
commands; uv loads `.env`, Django does not. Compose loads it separately and uses
database hostname `db`. In another terminal, run
`uv run --locked --env-file .env python manage.py db_worker --no-reload` to process
background jobs, including vehicle CSV preparation/publication and automatic
checks. Invitation mail is synchronous. See [background tasks](background-tasks.md)
for diagnostics, recovery, and pruning.

## Configuration

[`.env.example`](../.env.example) enables local debug and lists the required
secrets and optional Cloudflare settings. New cases begin with External AI off.
See [external AI configuration](analysis.md#external-ai-configuration) to enable
search, chat, or model processing.

| Variable | Behavior |
| --- | --- |
| `DJANGO_SECRET_KEY` | Required in every mode; generate a unique secret. |
| `DJANGO_DEBUG` | Defaults to `false`; `.env.example` enables local debug and console email. |
| `PANDECT_PUBLIC_ORIGIN` | Public HTTP(S) origin used in invitation emails. Compose defaults to `http://localhost:<PANDECT_WEB_PORT>`; host Django defaults to `http://localhost:8000`. No path, credentials, query, or fragment; override for another deployment address. |
| `DJANGO_ALLOWED_HOSTS` | Optional comma-separated override; defaults to the public-origin hostname and loopback names. |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Optional comma-separated override; defaults to the public origin. |
| `DJANGO_SECURE_HSTS_SECONDS` | Defaults to `86400` for an HTTPS public origin and `0` for HTTP. An explicit value overrides the default; Django must recognize HTTPS to emit the header. |
| `PANDECT_ALLOW_SIGNUPS` | Defaults to `true`; disable public registration with `false`. This also blocks registration by new invitees. |
| `PANDECT_TRUSTED_PROXY_COUNT` | Proxy hops trusted by allauth for client IP detection. Defaults to `0`. Set only behind a [controlled proxy chain](operations.md#first-dokploy-deployment). |
| `POSTGRES_DB`, `POSTGRES_USER` | Default to `pandect`. |
| `POSTGRES_PASSWORD` | Required by Compose; use a generated local password. |
| `POSTGRES_HOST`, `POSTGRES_PORT` | Host commands default to `127.0.0.1:5432`; `POSTGRES_PORT` also selects Compose's loopback database port. Containers use `db:5432`. |
| `PANDECT_WEB_PORT` | Compose's loopback web port for pages and case events, default `8000`. |
| `PANDECT_MEDIA_ROOT` | Private file storage root. Host default is `data/media` in the checkout. Compose defaults to `/app/data/media` inside the shared `app_data` mount. An override outside that mount needs its own private persistent mount shared by web and every worker. |
| `DJANGO_DEFAULT_FROM_EMAIL` | Optional sender override; defaults to `pandect@` followed by the public-origin hostname, or `pandect@localhost` for IP and `.localhost` origins. |
| `DJANGO_EMAIL_HOST`, `DJANGO_EMAIL_PORT` | SMTP host and port when debug is disabled; defaults `localhost:587`. |
| `DJANGO_EMAIL_HOST_USER`, `DJANGO_EMAIL_HOST_PASSWORD` | Operator SMTP credentials when debug is disabled. SMTP uses STARTTLS and a ten-second timeout. |

Empty host, CSRF-origin and sender overrides use their derived defaults.
Deployment-specific proxy trust, HTTPS redirect and HSTS settings are described
in [operations](operations.md#first-dokploy-deployment).

See [request handling](operations.md#upload-limits-and-request-handling) for
upload limits and [invitations and logs](operations.md#invitations-mail-and-private-logs)
for mail and private logging.

For port conflicts, set `POSTGRES_PORT` or `PANDECT_WEB_PORT` in `.env`. Containers
still use ports 5432 and 8000 internally. Align any explicit
`PANDECT_PUBLIC_ORIGIN` with the actual web address. Compose passes the variables
listed in [`compose.yaml`](../compose.yaml). Host and hosted processes read their
own environment. Keep configuration compatible across web and worker. If saved
execution settings no longer match, affected analysis stops recoverably and can
be submitted again.

Keep `.env`, databases, uploads, and backups out of Git. Put all test evidence and
validation output, including synthetic imports/exports, under ignored `data/`
or outside the checkout. Generate committed fixtures in code. See
[repository hygiene](validation.md#repository-hygiene) for commit/push hooks and limits.

## Manage dependencies

```sh
uv add <package>
uv add --dev <tool>
uv remove <package>
uv lock --upgrade-package <package>
uv sync --locked
```

Commit `pyproject.toml` and generated `uv.lock` together; never edit the lockfile
by hand. Use `uv lock --upgrade` for deliberate whole-graph updates. `--locked`
rejects stale lockfiles. Follow [architecture](architecture.md) for coordinated
runtime upgrades and run [validation](validation.md) before submission.
