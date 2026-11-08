# Local development

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) 0.13.6 and
Docker with Compose v2. uv manages Python, the `.venv`, and Python dependencies;
there is no requirements.txt or separate pip installation step. If you already
use [mise](https://mise.jdx.dev/), `mise install` installs the tools in `mise.toml`.
mise is optional.

## Pre-release database baseline

The domain-app reorganization requires a fresh database and empty queue. Existing
pre-release databases cannot be upgraded by applying the rewritten initial
migrations; do not use `--fake`. Use a new isolated Compose project/volumes or
new database, as described in [architecture](architecture.md#primary-keys-and-the-initial-schema).
Startup never deletes existing databases or media.

## First run

From the repository root:

```sh
uv sync --locked
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

After this setup, use `make run` whenever you want to start the website locally.
It builds the shared application image, starts PostgreSQL, the website, and a
background worker, and waits for web/database health and worker startup. Verify
actual queue execution with
`docker compose exec web python manage.py check_task_worker --timeout 30`.
Stop them with
`docker compose stop`; the database and evidence volumes are retained.

Open <http://localhost:8000/> (or your configured `PANDECT_WEB_PORT`). Create an
account and follow its email-verification link before opening case material.
The development container applies migrations on startup and runs Django's
development server. With `DJANGO_DEBUG=true`,
verification and invitation emails appear in `docker compose logs web`; these
messages contain private links, so keep development logs private. Production uses
the configured SMTP mailer instead.

Static assets use WhiteNoise in both the development server and production.
Debug mode serves source files with immediate cache revalidation; non-debug
builds use content-hashed filenames from `collectstatic`. Rebuild with `make run`
after changing templates or static assets in the container workflow. Host-based
debug development picks up source asset changes without collecting them.

After verification, **All cases → Create case** creates a case owned by you.
The case opens to **Evidence**. Use **Add evidence** to upload files, capture
original text, or record locations and communications. **Case tools** groups
People, Vehicles, and owner-only Settings. Choose **Settings → Invite collaborator**
to grant Viewer or Editor access. See
[cases](cases.md), [evidence](evidence.md), [people](people.md), and
[vehicles](vehicles.md) for workflow
details and limits. Ordinary investigators do not need staff permissions.
For Asset Location CSV exports, choose **Case tools → Vehicles → Import vehicle locations**,
then choose a case vehicle and check the export timezone (initially
`America/Toronto`). Preparation and confirmed imports run in the worker; refresh
the review to see progress and review case-vehicle mappings before confirming.
See [vehicle-location imports](vehicle-location-imports.md).

The optional `createsuperuser` command creates an operator for `/admin/`. Staff
permissions remain separate from product access; even superusers can inspect only
cases they own or share. `/health/` returns 200 when PostgreSQL is reachable and
503 otherwise. Compose waits for this readiness check. If operator administration
is needed, create that separate account with:

```sh
docker compose exec web python manage.py createsuperuser
```

## Run Python on the host

To use your editor and Django's autoreloader while keeping PostgreSQL in Docker,
install Tesseract and its English data on the host first. On macOS use
`brew install tesseract`; on Debian/Ubuntu use
`sudo apt-get install tesseract-ocr tesseract-ocr-eng`. Verify with
`tesseract --version` and `tesseract --list-langs` (which must include `eng`).
The Docker image already includes both packages. Then run:

```sh
docker compose stop web worker
docker compose up -d --wait db
uv run --locked --env-file .env python manage.py migrate
uv run --locked --env-file .env python manage.py runserver
```

Use `uv run --locked --env-file .env python manage.py <command>` for other
management commands. uv explicitly loads `.env`; Django does not implicitly load
files. Compose reads `.env` itself and sets the database hostname to `db`.
Run `uv run --locked --env-file .env python manage.py db_worker --no-reload`
in another terminal to process tasks while using the host web server. See
[background tasks](background-tasks.md) for diagnostics, recovery, and pruning.
Existing invitation and import workflows do not enqueue tasks yet.

## Configuration

Optional private text/transcription runtimes use the `local-ai` Compose profile.
They require reviewed local model files, hashes and a runtime token; ordinary
startup does not download models. Follow [local analysis](local-analysis.md) for
setup, explicit cloud selection, capacity measurement and model qualification.

| Variable | Behavior |
| --- | --- |
| `DJANGO_SECRET_KEY` | Required in every mode; generate a unique secret. |
| `DJANGO_DEBUG` | Defaults to `false`; `.env.example` enables local debug and console email. |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated hosts; defaults to loopback names only. |
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

Keep `.env`, databases, uploads, and backups out of Git. Existing legacy runtime
data is neither imported nor changed by this setup.

## Manage dependencies

```sh
uv add <package>
uv add --dev <tool>
uv remove <package>
uv lock --upgrade-package <package>
uv sync --locked
```

Commit `pyproject.toml` and the generated `uv.lock` together. Do not edit the
lockfile by hand. Use `uv lock --upgrade` for a deliberate whole-graph update.
`uv sync --locked` and `uv run --locked` reject stale lockfiles rather than
silently resolving different versions. Runtime pins and coordinated upgrades are
explained in [architecture](architecture.md); run the [checks](validation.md)
before submitting changes.
