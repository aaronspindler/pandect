# Local development

Install Docker with Compose v2 and
[uv](https://docs.astral.sh/uv/getting-started/installation/) at the version in
[`mise.toml`](../mise.toml). uv manages Python, `.venv`, and dependencies; no
requirements.txt or separate pip step is needed. Optional
[mise](https://mise.jdx.dev/) users can run `mise install` for the pinned tools.

## Pre-release database baseline

The domain-app reorganization requires a fresh database and empty queue. Use a
new isolated Compose project/volumes or database; rewritten initial migrations
cannot upgrade old pre-release databases, including with `--fake`. See
[architecture](architecture.md#primary-keys-and-the-initial-schema). Startup never
deletes existing databases or media.

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

Use `make run` for subsequent starts. It builds the shared image, starts
PostgreSQL, web, and worker, and waits for web/database health and worker startup.
Verify queue execution with
`docker compose exec web python manage.py check_task_worker --timeout 30`.
`docker compose stop` retains database and evidence volumes.

Open <http://localhost:8000/> (or `PANDECT_WEB_PORT`), create an account, and verify
its email before opening case material. The development container migrates on
startup and runs Django's development server. With `DJANGO_DEBUG=true`, private
verification/invitation links appear in `docker compose logs web`; keep those
logs private. Production uses SMTP.

WhiteNoise serves assets in development and production. Debug mode revalidates
source files immediately; non-debug builds use `collectstatic` content hashes.
After template/static changes, rebuild containers with `make run`. Host debug
development picks up source assets without collection.

After verification, create a case from **All cases** and use **Add evidence**.
Ordinary investigators do not need staff permissions. See
[case workflows](cases.md), [evidence](evidence.md), and
[vehicle-location imports](vehicle-location-imports.md) for task-specific guidance.

Staff permissions are separate from product access; even superusers can inspect
only cases they own or share. Compose waits for `/health/`: 200 when PostgreSQL
is reachable, 503 otherwise. To create an optional `/admin/` operator:

```sh
docker compose exec web python manage.py createsuperuser
```

## Run Python on the host

For Django's host autoreloader with PostgreSQL in Docker, first install Tesseract
and English data: `brew install tesseract` on macOS, or
`sudo apt-get install tesseract-ocr tesseract-ocr-eng` on Debian/Ubuntu. Check
`tesseract --version` and that `tesseract --list-langs` includes `eng`. The
application image already includes both packages. Then run:

```sh
docker compose stop web worker
docker compose up -d --wait db
uv run --locked --env-file .env python manage.py migrate
uv run --locked --env-file .env python manage.py runserver
```

Use `uv run --locked --env-file .env python manage.py <command>` for management
commands; uv loads `.env`, Django does not. Compose loads it separately and uses
database hostname `db`. In another terminal, run
`uv run --locked --env-file .env python manage.py db_worker --no-reload` to process
background jobs, including vehicle CSV preparation/publication and automatic
checks. Invitation mail is synchronous. See [background tasks](background-tasks.md)
for diagnostics, recovery, and pruning.

## Configuration

Optional text/transcription runtimes use the `local-ai` profile and require
reviewed local model files, hashes, and a runtime token. Startup never downloads
models. See [local analysis](local-analysis.md) for setup, cloud selection,
capacity measurement, and qualification.

| Variable | Behavior |
| --- | --- |
| `DJANGO_SECRET_KEY` | Required in every mode; generate a unique secret. |
| `DJANGO_DEBUG` | Defaults to `false`; `.env.example` enables local debug and console email. |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated hosts; defaults to loopback names only. |
| `PANDECT_ALLOW_SIGNUPS` | Defaults to `true`; disable public registration with `false`. This also blocks registration by new invitees. |
| `PANDECT_TRUSTED_PROXY_COUNT` | Proxy hops trusted by allauth for client IP detection. Defaults to `0`. Set only behind a [controlled proxy chain](operations.md#first-dokploy-deployment). |
| `POSTGRES_DB`, `POSTGRES_USER` | Default to `pandect`. |
| `POSTGRES_PASSWORD` | Required by Compose; use a generated local password. |
| `POSTGRES_HOST`, `POSTGRES_PORT` | Host commands default to `127.0.0.1:5432`; Compose uses `db:5432`. |
| `PANDECT_DB_PORT` | Compose's loopback database port, default `5432`. |
| `PANDECT_WEB_PORT` | Compose's loopback web port, default `8000`. |
| `PANDECT_MEDIA_ROOT` | Optional private original-storage path; defaults to `data/media`. Originals are served only through case-authorized download routes. |
| `PANDECT_PUBLIC_ORIGIN` | Public HTTP(S) origin used in invitation emails, default `http://localhost:8000`; no path, credentials, query, or fragment. Match the actual deployment address/port. |
| `PANDECT_INVITATION_DAYS` | Positive invitation lifetime in days, default `7`. |
| `PANDECT_MAX_FILE_BYTES` | Positive per-file limit, default `104857600` (100 MiB), enforced while streaming and before storage. |
| `PANDECT_MAX_TEXT_BYTES` | Positive captured-text UTF-8 limit, default `1048576` (1 MiB). |
| `PANDECT_MAX_BATCH_FILES` | Positive browser-queue size limit, default `20`; files are sent sequentially, one per request. |
| `DJANGO_DEFAULT_FROM_EMAIL` | Sender address, default `pandect@localhost`. |
| `DJANGO_EMAIL_HOST`, `DJANGO_EMAIL_PORT` | SMTP host and port when debug is disabled; defaults `localhost:587`. |
| `DJANGO_EMAIL_HOST_USER`, `DJANGO_EMAIL_HOST_PASSWORD` | Operator SMTP credentials when debug is disabled. SMTP uses STARTTLS and a ten-second timeout. |

For a port conflict, change `PANDECT_DB_PORT` and `POSTGRES_PORT` to the same unused
port in `.env`, and/or change `PANDECT_WEB_PORT`. Update `PANDECT_PUBLIC_ORIGIN`
when changing the externally visible web port. The containers always use 5432 and
8000 internally. Compose is a local development configuration and passes only
the variables it declares; hosted configuration is supplied directly to Django.

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
