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

The local example contains only the two required secrets and `DJANGO_DEBUG=true`.
Database names, ports and internal processing limits have defaults; add optional
environment values only for your deployment or an explicitly configured service.

Optional local text processing uses an operator-controlled Ollama installation
with a reviewed Qwen3.5 model, manifest digest and exact server-version pin.
The separate `local-ai` Compose profile supplies only Whisper transcription and
requires reviewed model weights, a file hash and a runtime token. Startup never
downloads models. Printed-English OCR remains in the bounded Tesseract parser.
See [local analysis](local-analysis.md) for setup, cloud selection, capacity
measurement, qualification and upgrading older local text configurations.

| Variable | Behavior |
| --- | --- |
| `DJANGO_SECRET_KEY` | Required in every mode; generate a unique secret. |
| `DJANGO_DEBUG` | Defaults to `false`; `.env.example` enables local debug and console email. |
| `PANDECT_PUBLIC_ORIGIN` | Public HTTP(S) origin used in invitation emails. Compose defaults to `http://localhost:<PANDECT_WEB_PORT>`; host Django defaults to `http://localhost:8000`. No path, credentials, query, or fragment; override for another deployment address. |
| `DJANGO_ALLOWED_HOSTS` | Optional comma-separated override; defaults to the public-origin hostname and loopback names. |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Optional comma-separated override; defaults to the public origin. |
| `PANDECT_ALLOW_SIGNUPS` | Defaults to `true`; disable public registration with `false`. This also blocks registration by new invitees. |
| `PANDECT_TRUSTED_PROXY_COUNT` | Proxy hops trusted by allauth for client IP detection. Defaults to `0`. Set only behind a [controlled proxy chain](operations.md#first-dokploy-deployment). |
| `POSTGRES_DB`, `POSTGRES_USER` | Default to `pandect`. |
| `POSTGRES_PASSWORD` | Required by Compose; use a generated local password. |
| `POSTGRES_HOST`, `POSTGRES_PORT` | Host commands default to `127.0.0.1:5432`; `POSTGRES_PORT` also selects Compose's loopback database port. Containers use `db:5432`. |
| `PANDECT_WEB_PORT` | Compose's loopback web port, default `8000`. |
| `PANDECT_MEDIA_ROOT` | Optional private original-storage path; defaults to `data/media`. Originals are served only through case-authorized download routes. |
| `DJANGO_DEFAULT_FROM_EMAIL` | Optional sender override; defaults to `pandect@` followed by the public-origin hostname, or `pandect@localhost` for IP and `.localhost` origins. |
| `DJANGO_EMAIL_HOST`, `DJANGO_EMAIL_PORT` | SMTP host and port when debug is disabled; defaults `localhost:587`. |
| `DJANGO_EMAIL_HOST_USER`, `DJANGO_EMAIL_HOST_PASSWORD` | Operator SMTP credentials when debug is disabled. SMTP uses STARTTLS and a ten-second timeout. |

Empty host, CSRF-origin and sender overrides use their derived defaults.
Deployment-specific proxy trust, HTTPS redirect and HSTS settings are described
in [operations](operations.md#first-dokploy-deployment).

Files allow 1 GiB each, captured text allows 10 MiB UTF-8, and browser batches
allow 100 sequential uploads. Invitations expire after seven days. These are
application defaults in `config/settings.py`, rather than environment variables.
Specialized imports and parsers retain their separate bounds; accepting an
original does not mean every parser can process it. See
[request limits](operations.md#upload-limits-and-request-handling).

For a port conflict, change `POSTGRES_PORT` to an unused port in `.env`, and/or
change `PANDECT_WEB_PORT`. Compose derives its default public origin from the
published web port, so a port-only change needs no separate origin override.
If `PANDECT_PUBLIC_ORIGIN` is explicitly configured, keep it aligned with the
actual address; host-based Django on another port also needs this override.
The containers always use 5432 and 8000 internally. Compose is a local development
configuration and passes only
the variables it declares; hosted configuration is supplied directly to Django.

Remove obsolete internal tuning values from existing environments; they no longer
override application defaults. Restart web and worker together with matching
configuration. Queued analysis whose saved execution settings differ fails
recoverably; start a new run without changing retained results or source material.

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
