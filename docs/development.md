# Local development

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) 0.13.5 and
Docker with Compose v2. uv manages Python, the `.venv`, and Python dependencies;
there is no requirements.txt or separate pip installation step. If you already
use [mise](https://mise.jdx.dev/), `mise install` installs the tools in `mise.toml`.
mise is optional.

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
docker compose up --build --wait
docker compose exec web python manage.py createsuperuser
```

Open <http://localhost:8000/accounts/login/> or <http://localhost:8000/admin/>.
`/` redirects to sign-in. `/health/` returns 200 only when PostgreSQL is reachable,
and 503 when it is unavailable. Compose waits for this readiness check.

The development container applies migrations on startup and runs Django's
development server. Registration requires email confirmation; development emails
appear in `docker compose logs web`. The superuser command creates an operator for
Django admin, whose staff authentication is separate from allauth email verification.
In admin, use **Cases → Add case** to create a case owned by the signed-in user.
Save it before adding collaborators. Staff access and Django model permissions
remain required; even superusers see only cases they own or share. See
[case administration](cases.md) for permissions and membership management.
The investigator-facing interface is planned.

## Run Python on the host

To use your editor and Django's autoreloader while keeping PostgreSQL in Docker:

```sh
docker compose stop web
docker compose up -d --wait db
uv run --locked --env-file .env python manage.py migrate
uv run --locked --env-file .env python manage.py runserver
```

Use `uv run --locked --env-file .env python manage.py <command>` for other
management commands. uv explicitly loads `.env`; Django does not implicitly load
files. Compose reads `.env` itself and sets the database hostname to `db`.

## Configuration

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
| `PANDECT_MEDIA_ROOT` | Optional filesystem storage path; defaults to `data/media`. No download route exists yet. |
| `DJANGO_DEFAULT_FROM_EMAIL` | Sender address, default `pandect@localhost`. |
| `DJANGO_EMAIL_HOST`, `DJANGO_EMAIL_PORT` | SMTP host and port when debug is disabled; defaults `localhost:587`. |
| `DJANGO_EMAIL_HOST_USER`, `DJANGO_EMAIL_HOST_PASSWORD` | Operator SMTP credentials when debug is disabled. SMTP uses STARTTLS and a ten-second timeout. |

For a port conflict, change `PANDECT_DB_PORT` and `POSTGRES_PORT` to the same unused
port in `.env`, and/or change `PANDECT_WEB_PORT`. The containers always use 5432
and 8000 internally. Compose is a local development configuration and passes only
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
