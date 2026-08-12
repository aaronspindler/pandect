# Local operations

Pandect's supported runtime is Docker Compose on one trusted operator's
machine. Run the commands in this guide from the repository root.

> [!WARNING]
> Pandect has no authentication or authorization. Keep the published web port
> on loopback (`127.0.0.1`). Do not expose the default stack to a LAN or the
> public internet. An operator who changes that boundary must first provide and
> verify an external access-control layer.

## First run

Start Docker, then run:

```console
make setup && make up
```

Setup preserves existing configuration and data and is safe to repeat for the
same source revision. It creates `.env` from the documented example when needed
with mode `0600` and the current host user and group IDs, creates the persistent
data directory with mode `0700`, builds the application image, and applies
migrations. It does not replace an existing `.env` or erase persistent state.
Stop a running stack before using setup with changed code, as required by the
upgrade procedure below. Setup must run as an unprivileged user. Django
generates the installation secret inside the persistent directory during the
one-off migration command unless `.env` supplies an explicit override.

The application is available at <http://127.0.0.1:8000/>. The default stack
does not require PostgreSQL, Redis, Node.js, third-party credentials, or an
internet connection after its image and locked dependencies have been built.

## Local configuration

Setup creates `.env` from `.env.example` only when `.env` is absent. The small
configuration surface is:

| Setting | Local behaviour |
| --- | --- |
| `PANDECT_DATA_DIR` | Host persistent-data path; defaults to `./data`. Compose mounts it at `/data` in the `web` and `worker` services. Relative and absolute host paths are supported. |
| `PANDECT_PORT` | Host web port; defaults to `8000`. Compose always binds it to `127.0.0.1`. |
| `PANDECT_UID`, `PANDECT_GID` | Container user and group IDs. Make uses the invoking user's IDs, and setup records them in a new `.env` for direct Compose debugging, so bind-mounted state remains writable without running as root. |
| `PANDECT_SECRET_KEY` | Optional explicit Django secret override. When omitted, setup generates `secret.key` in the persistent data directory and settings use that file. |
| `PANDECT_DEBUG` | Boolean Django debug setting; defaults to `false` in Compose. |
| `PANDECT_ALLOWED_HOSTS` | Comma-separated Django host-header allowlist; defaults to `localhost,127.0.0.1,[::1]`. |
| `PANDECT_TIME_ZONE` | Django time zone; defaults to `UTC`. |

Changing `PANDECT_ALLOWED_HOSTS` is not authentication and does not make public
exposure safe. Keep Compose's loopback binding. `.env` is local, ignored state;
never commit it if it contains a secret or machine-specific configuration.
After editing runtime settings, use `make down && make up` so Compose recreates
the services; `make restart` does not recreate containers.

## Command reference

`make help` is the authoritative, discoverable list of public targets. The
supported commands are:

| Command | Purpose |
| --- | --- |
| `make help` | List public commands, descriptions, and state-changing behaviour. |
| `make setup` | Create local configuration and data paths, build the image, and apply migrations. |
| `make up` | Start the web and worker services in the background. |
| `make down` | Stop and remove containers while preserving persistent data. |
| `make restart` | Restart the supported services. |
| `make logs` | Follow web and worker logs; press Control-C to stop following without stopping services. |
| `make ps` | Show service state and health. |
| `make shell` | Open an interactive Django application shell in a one-off container; commands entered there may mutate persistent data. |
| `make manage ARGS="..."` | Run a Django management command, for example `make manage ARGS="showmigrations"`. |
| `make migrate` | Apply committed Django migrations explicitly. |
| `make makemigrations` | Create migrations through the tools container; mutates source and Docker cache. Review generated files. |
| `make task-smoke` | Prove enqueue, worker execution, and result retrieval end to end. |
| `make test` | Run the hermetic pytest suite in the tools container; may populate disposable and Docker caches. |
| `make lint` | Check Python formatting and Ruff lint rules in the tools container; may populate caches. |
| `make format` | Format Python source through the tools container; mutates source and may populate caches. Review the diff. |
| `make validate` | Run Django and migration checks, validate Compose, and build the runtime image; may populate disposable and Docker caches. |
| `make check` | Run the complete CI validation suite; may populate disposable and Docker caches. |
| `make clean` | Remove disposable build and test artifacts. It does not delete Pandect data. |

Docker Compose remains directly usable for diagnosis. For example,
`docker compose logs --tail=200 worker` shows only recent worker logs. Prefer
the Make targets for normal operation so service names and configuration stay
centralized.

## Web and worker responsibilities

The two long-running Compose services use the same application image and
persistent data directory, but have separate roles:

- `web` runs Django through Gunicorn, serves server-rendered pages and local
  static assets, accepts loopback-only HTTP traffic, and enqueues background
  work.
- `worker` runs the Huey consumer with conservative local concurrency. It
  executes queued and future periodic tasks. It does not serve HTTP or apply
  migrations.

Migrations are explicit: neither long-running service applies them at startup.
Run `make migrate` (or the setup procedure) before starting code that expects a
new schema.

`make task-smoke` enqueues a side-effect-free test task, waits for the worker
with a bounded timeout, verifies its result, and returns nonzero if the full
path is unavailable. Automated tests instead use eager task execution and do
not need a running worker.

Future background tasks should be idempotent where practical, use bounded
timeouts, retry only failures that are safe and likely to be transient, and
surface terminal failures in worker logs. A schedule belongs in explicit Huey
configuration; the foundation does not ship source polling or domain jobs.

## Persistent local state

`PANDECT_DATA_DIR` selects the one persistent data directory and defaults to
`./data` in the repository. It contains separate stores:

- `application.sqlite3` is Django's application database.
- `huey.sqlite3` is Huey's queue, schedule, and result database.
- `media/` contains locally stored catalogued image and video files.
- `secret.key` is the generated local Django secret unless
  `PANDECT_SECRET_KEY` is supplied explicitly.

The `web` and `worker` services see the same directory at `/data`. The two
SQLite files must remain separate. The directory may also gain SQLite `-wal`
and `-shm` sidecar files while a database is open; those are part of live
database state.

Treat the whole directory as sensitive and persistent. It is ignored by Git.
`make down`, container recreation, `make clean`, and an application-image
rebuild preserve it. Never commit it, copy an individual open SQLite file, or
edit either database by hand.

## Backup and restore

Back up the **entire persistent data directory with `web` and `worker` stopped**.
Stopping first gives the application database, Huey queue, sidecar files, and
generated secret a consistent boundary. Close any direct Django shell, SQLite
client, or other process using the directory too.

For the default `./data` path:

```console
make down
mkdir -p ../pandect-backups
tar -czf "../pandect-backups/pandect-data-$(date +%Y%m%d-%H%M%S).tar.gz" data
```

This writes the archive outside the repository. Store it somewhere protected.
If `PANDECT_DATA_DIR` in `.env` points elsewhere, archive that entire directory
instead of `data`. Keep its directory contents and permissions together.
The default generated secret is included. If you explicitly put
`PANDECT_SECRET_KEY` in `.env`, preserve that secret separately and securely;
the data-directory archive does not contain the override.

To restore the default path, first stop the stack and preserve the current
directory under an unused name. The verification commands below also assume
the default port `8000`; substitute the configured `PANDECT_PORT` if it differs.

```console
make down
if [ -d data ]; then mv data "../pandect-data-before-restore-$(date +%Y%m%d-%H%M%S)"; fi
tar -xzf ../pandect-backups/pandect-data-YYYYMMDD-HHMMSS.tar.gz
make setup
make up
curl --fail http://127.0.0.1:8000/healthz/
curl --fail http://127.0.0.1:8000/readyz/
make task-smoke
```

The archive made above contains the top-level `data` directory, so extract it
from the repository root. For a custom `PANDECT_DATA_DIR`, restore to that same
configured path. Do not merge a backup into a populated data directory. Keep
the pre-restore directory until the web health check and task smoke both pass.

## Upgrade

Use a source revision, database contents, and migrations that belong together.
The commands below use the default `./data` path and port `8000`. If
`PANDECT_DATA_DIR` is customized, change the archive command to capture that
entire directory and verify the archive **before** pulling code or applying
migrations. Substitute the configured `PANDECT_PORT` in verification URLs too.

```console
make down
mkdir -p ../pandect-backups
tar -czf "../pandect-backups/pandect-data-before-upgrade-$(date +%Y%m%d-%H%M%S).tar.gz" data
git pull --ff-only
make setup
make up
curl --fail http://127.0.0.1:8000/healthz/
curl --fail http://127.0.0.1:8000/readyz/
make task-smoke
```

`make setup` rebuilds the shared image from the locked dependencies and applies
committed migrations before services start. Read release notes before upgrading
in case a future release adds a specific step. Do not run new code against an
old schema or try to downgrade a migrated database in place. Rollback means
stopping the stack, restoring the complete pre-upgrade data backup, and
returning to its matching source revision.

## Logs, health, and validation

Follow both services:

```console
make logs
```

Inspect one service without following indefinitely:

```console
docker compose logs --tail=200 web
docker compose logs --tail=200 worker
```

Logs go to standard container output. They should explain failures without
dumping settings, secret values, or collected data; handle any local log copies
as sensitive.

The dependency-free liveness endpoint proves that Django can respond:

```console
curl --fail http://127.0.0.1:8000/healthz/
```

The readiness endpoint also verifies that Django can query the application
SQLite database:

```console
curl --fail http://127.0.0.1:8000/readyz/
```

It returns HTTP 503 with a sanitized response when the database is unavailable.
Use `make ps` for Compose liveness health and `make task-smoke` for the
queue/worker path. Neither a liveness response nor database readiness proves
that the worker is executing tasks.

Before submitting a change, run the complete local validation:

```console
make check
```

The narrower `make test`, `make lint`, and `make validate` targets match the
separately reported CI steps and are useful during development. `make validate`
performs Django system checks, missing-migration detection, Compose validation,
and the application-image build. `make check` runs all three targets. Test,
lint, format, and complete validation run in a short-lived, profile-gated
`tools` container with Python and the frozen development dependencies. They do
not require host-installed Python or `uv`. Their first run may need network
access to obtain base images and locked dependencies, and it populates Docker's
build cache; the tools service is not part of the long-running default stack.

## Troubleshooting

### Docker is unavailable or permission is denied

Run `docker info` and `docker compose version`. If the daemon is unavailable,
start Docker Desktop or Docker Engine. On Linux, follow your distribution's
documented Docker access setup and start a new login session after changing
group membership. Membership in Docker's group is effectively root-level host
access; grant it deliberately. Avoid running individual Make commands with
`sudo`, which can leave `.env` or `data` owned by root.

If the checkout or restored `.env` came from another machine, update
`PANDECT_UID` and `PANDECT_GID` to the output of `id -u` and `id -g`, then run
`make down && make up` to recreate the services with the correct identity. Do
not make the data directory world-writable to bypass an ownership mismatch.

### Port 8000 is already occupied

If `lsof` is available, identify the local listener:

```console
lsof -nP -iTCP:8000 -sTCP:LISTEN
```

Otherwise, use your operating system's port-listener tool. Stop or reconfigure
the conflicting process. Alternatively, set an unused port in `.env`, for
example `PANDECT_PORT=8001`, then run
`make down && make up` and use `http://127.0.0.1:8001/`. Compose retains the
loopback binding when this port changes; do not change it to `0.0.0.0` as a
shortcut.

### SQLite reports `database is locked`

Pandect configures a five-second busy timeout for brief contention, not high
concurrency. Use `make ps` to confirm only the supported web and single worker
are running, and stop any direct Django shells, SQLite clients, backup jobs, or
duplicate stacks using the same directory. Let active work finish, then use
`make restart`. Never copy, replace, or delete an open database to clear a lock.
Persistent or frequent lock timeouts indicate a workload outside the
foundation's supported limited-concurrency model; capture the web and worker
logs before changing configuration.

### The worker is not processing tasks

Run:

```console
make ps
docker compose logs --tail=200 worker
make task-smoke
```

Confirm that `worker` is running from the same Compose project and shares the
same configured data directory as `web`. Correct the reported startup or task
error and run `make restart`. Repeating `make task-smoke` while the worker is
down only adds work to the persistent queue; it does not repair the worker.

### The Huey queue file is stale or corrupt

First rule out a stopped worker or a normal backlog. Queue recovery discards
pending, scheduled, retry, and stored result state, so use it only when logs
identify `huey.sqlite3` itself as unrecoverable and that work can be abandoned.
Stop the stack, back up the whole persistent directory, and move (do not
delete) `huey.sqlite3` plus any matching `huey.sqlite3-wal` and
`huey.sqlite3-shm` sidecars out of the data directory. Leave
`application.sqlite3` and the secret-key file untouched. Start the stack and
run `make task-smoke`; Huey will create a clean queue store. Retain the moved
files until recovery is verified.

For the default data path, after making the backup described above:

```console
make down
PANDECT_QUEUE_RECOVERY="../pandect-queue-recovery-$(date +%Y%m%d-%H%M%S)"
mkdir "$PANDECT_QUEUE_RECOVERY"
for file in data/huey.sqlite3 data/huey.sqlite3-wal data/huey.sqlite3-shm; do
  if [ -e "$file" ]; then mv "$file" "$PANDECT_QUEUE_RECOVERY/"; fi
done
make up
make task-smoke
```

### A migration fails

Do not start the new services or edit Django's migration table manually. Save
the complete `make migrate` output, then inspect state with:

```console
make manage ARGS="showmigrations"
```

Confirm the checkout is complete, `.env` still points at the intended data
directory, and the data directory is writable by the container user. Fix the
reported cause and rerun `make migrate`. If the release cannot be repaired,
stop the stack and restore the complete pre-upgrade backup together with its
matching source revision. Never recover by deleting only
`application.sqlite3`; doing so destroys application state and breaks the
backup boundary.

## Architecture boundary

[`architecture/0001-foundation.md`](architecture/0001-foundation.md) is the
canonical decision. The supported foundation is a single-machine,
limited-concurrency, server-rendered Django application using two local SQLite
stores. It is not an internet-facing or multi-operator deployment design.

Domain entities, cases, relationships, evidence graphs, timelines, search,
identity resolution, source adapters, scraping, polling, browser automation,
and source credentials are explicit non-goals of the foundation. PostgreSQL,
Redis, Celery, cloud-managed services, remote telemetry, a public API, and a
general plugin framework are also excluded by default.
