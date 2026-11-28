# Compose operation and recovery

Use [local development](development.md#first-run) for setup and startup. Compose
binds application and database ports to loopback. Its development settings, console
email, and debug configuration are for local use.

```sh
docker compose ps
docker compose logs --tail 100 web worker db
docker compose stop
docker compose down
```

`stop` and `down` retain `postgres_data` (PostgreSQL) and `app_data` (private
evidence). Never use `down --volumes` on retained data.

The local Compose web command runs pending schema migrations after database
readiness and before starting the ASGI web server. `/health/` checks
PostgreSQL. On startup failure, inspect the logs and configuration, then rerun
`make run`. Rotate existing database passwords in PostgreSQL before updating
`.env`. After startup, run the [queue execution diagnostic](background-tasks.md#startup-and-diagnostics).

Compose sets `PGOPTIONS=-c jit=off` for web queries and gives PostgreSQL 512 MiB
of dynamic shared memory. If PostgreSQL reports `could not resize shared memory
segment`, inspect `docker compose exec -T db df -h /dev/shm`. Capacity there is
separate from free space on the database volume.

## Database backup and restoration

### Backup contents

Keep coordinated database and private-file backups encrypted off-host. Record
the application revision, PostgreSQL major version, and private storage roots
with the snapshot. The database holds accounts, case/evidence metadata,
captured text, and private task arguments and results. With default Compose
storage, `app_data` holds originals and derived files.

### Capture a matching backup

Use these commands when all private evidence for the database is under the
Compose `app_data` volume. If the database also uses host media, other mounts,
or external storage, coordinate an archive of every such root during the same
write outage and verify it before marking the snapshot complete. Stop any
additional writers, including host web/worker processes and parallel workers,
before capturing the database and files.

Run from the active local Compose checkout. The stop commands prevent new web
writes and let both worker queues finish their active task before exiting.
Each attempt uses a new owner-only directory.

```sh
(
  set -eu
  umask 077
  snapshot=$(mktemp -d "$HOME/pandect-backup.XXXXXXXX")
  printf 'Snapshot directory: %s\n' "$snapshot"

  docker compose stop web
  docker compose --profile instagram-pilot stop --timeout -1 worker instagram-worker
  docker compose exec -T db sh -c \
    'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$snapshot/database.dump"
  docker compose run --rm --no-deps -T web tar -C /app/data -cf - . \
    > "$snapshot/app-data.tar"

  docker compose exec -T db pg_restore --list < "$snapshot/database.dump" > /dev/null
  tar -tf "$snapshot/app-data.tar" > /dev/null
  touch "$snapshot/COMPLETE"
)
```

Use the snapshot after both archive checks pass and `COMPLETE` exists. A capture
or check failure leaves the snapshot incomplete. Inspect the failure and retry
in a fresh directory. For a backup during normal operation, restart with
`docker compose up --wait` using the installation's usual profiles after
inspection. During an upgrade, keep the writers stopped until the upgrade
procedure says to restart them. Copy complete snapshots off-host and test
restoration to qualify recovery.

### Restore in a separate Compose project

Use a trusted completed snapshot and matching application revision/PostgreSQL
major version. The example restores the default Compose volume layout. Restore
every other recorded private storage root and preserve its storage keys before
starting web. Follow [local setup](development.md) for a separate owner-only
recovery environment with fresh secrets, closed signups, and external services
unset. Clear production Django/PostgreSQL/Pandect/Compose shell overrides and use
a new project name for fresh database/file volumes.

```sh
(
  set -eu
  snapshot=/absolute/path/to/completed-snapshot
  recovery_env=/absolute/path/to/recovery.env
  recovery_project=pandect-recovery-$(date -u +%Y%m%d%H%M%S)

  recovery() {
    POSTGRES_PORT=0 PANDECT_WEB_PORT=0 \
      docker compose -f compose.yaml --env-file "$recovery_env" -p "$recovery_project" "$@"
  }

  test -f "$snapshot/COMPLETE"
  existing_volumes=$(docker volume ls -q --filter "label=com.docker.compose.project=$recovery_project")
  test -z "$existing_volumes"
  printf 'Recovery project: %s\n' "$recovery_project"

  recovery up -d --wait db
  recovery exec -T db sh -c \
    'pg_restore --single-transaction --no-owner --no-privileges -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
    < "$snapshot/database.dump"
  recovery run --build --rm --no-deps -T web tar -C /app/data -xf - \
    < "$snapshot/app-data.tar"
  recovery run --rm --no-deps -T web python manage.py migrate --check

  recovery up -d --wait web
  recovery port web 8000
)
```

Web starts only after both restores succeed, on random loopback ports. Verify
records and original checksums. On failure, inspect the isolated project and retry
in a fresh one. Retain the original project/volumes until recovery is qualified.
To stop recovery, repeat its Compose flags with `stop`.

Before enabling workers or resuming use, inspect queued/running tasks for replay
of external effects. Follow [background recovery](background-tasks.md) and run the
worker diagnostic after startup. External storage needs coordinated snapshots,
restoration, and preserved storage keys. See
[evidence lifecycle and cleanup recovery](evidence.md#private-storage-and-deletion).

## Upgrades and hosted deployment

Before upgrading, [capture a matching backup](#capture-a-matching-backup) and
leave all writers stopped until migration and verification finish. If the old
Compose project has separate `proxy` and `events` services, stop them with
the old Compose file so web can bind the loopback port. On hosted upgrades,
route `/events/` to the web Application. Inspect
`migrate --plan` and the migration files for schema locks, retained-data
backfills, and rollback constraints. Catalogue backfills use saved results
and can take longer with more findings. For an interrupted concurrent index
build, check whether an invalid index remains before retrying. Earlier
application images may be incompatible after a migration. If reversing the
full migration plan would remove retained data or cannot be done safely,
qualify restoration from the matching backup in a separate environment.
For rollback to a worker that predates case approval, cancel queued external
work or disable outbound analysis before starting that worker.

When moving a PostgreSQL 18 installation from the former vector-based search,
keep the same data directory, volume, and architecture. Before migration, record
the running app and database images and the retained evidence, findings, reviews,
and chat inventory. Restore the paired database and media backup in isolation
with those images. Compare inventory and original checksums, and reopen saved
citations before proceeding. The hosted-search migration removes rebuildable
search data, disables External AI, and preserves evidence, findings, reviews,
and saved chat. Verify the retained inventory and citations before the owner
reviews and enables the new destinations. For rollback, restore the same paired
backup with the previous app and database images. If other
applications share the vector extension, keep a compatible database image or
move those applications before switching to the standard image. For an older
pre-release schema, follow the [fresh baseline](architecture.md#primary-keys-and-the-initial-schema).

After reviewing dependency and runtime compatibility, select the installation's
usual Compose profiles and run:

```sh
docker compose build
docker compose run --rm --no-deps web python manage.py migrate --noinput
```

Verify the migrated inventory and original checksums, then run
`docker compose up --no-build --wait` with the usual profiles and check worker
execution.

Gunicorn serves the application; WhiteNoise serves build-collected public assets.
Rebuild after template/static changes and deploy matching templates/hashed-asset
manifests. Host production installs need `python manage.py collectstatic --noinput`
before serving. Never publish private evidence through a proxy or public bucket.

`compose.yaml` is for local development; use the [Dokploy recipe](#first-dokploy-deployment)
for hosting. Other hosts need HTTPS, allowed hosts, secure secrets, SMTP, matching
`PANDECT_PUBLIC_ORIGIN`, private persistent storage, database backups, and migration
before web/worker startup. Debug defaults off, requiring HTTPS session/CSRF cookies.
Run `python manage.py check --deploy` with actual configuration and resolve findings.
Trust proxy headers only behind a controlled proxy.

## First Dokploy deployment

After CI checks pass, CD publishes one multi-platform GHCR image and resolves its
immutable digest. Dokploy runs native **worker** and **web** Applications plus
managed **PostgreSQL**, without Compose.

1. In the production project, create a managed PostgreSQL resource using the
   digest-pinned PostgreSQL image from the `db` service in [Compose](../compose.yaml),
   with a new database, role, and strong password.
   Keep it private; use its internal hostname and port 5432 in the application
   configuration. For a new PostgreSQL 18 resource, mount a named volume at
   `/var/lib/postgresql` and verify that `SHOW data_directory` points inside
   it. Check the mount explicitly when using a custom image. For an existing
   resource whose volume is mounted directly at `/var/lib/postgresql/18/docker`,
   preserve that exact volume and mount during the upgrade.
2. Create native Docker Applications named `web` and `worker` on the
   intended deployment server. Configure one replica each. Use the same
   `ghcr.io/<owner>/<repository>@sha256:<digest>` image for both.
   Leave web's Command and Arguments overrides empty so it uses the image's
   default Gunicorn command. Set worker's Command to `sh` and Arguments to the
   single entry `/app/scripts/docker-entrypoint-worker.sh`. Configure the web
   Application's native HTTP health check against
   `http://127.0.0.1:8000/health/`; keep worker rollout stop-first and web start-first.
3. Copy [`.env.dokploy.example`](../.env.dokploy.example) into the web and worker
   environments, replacing blank secrets and the database hostname. Debug defaults
   off; this example closes signups and uses
   `PANDECT_PUBLIC_ORIGIN=https://openpandect.com`. The public origin supplies the
   default allowed host, CSRF origin and email sender; optional explicit overrides
   support other deployment requirements.
4. Mount the same private persistent named volume at `/app/data` in web and
   worker. Keep them on the same server and storage-owning Swarm node;
   local named volumes are not shared between nodes. The application UID must
   own the mount. No public route may serve that directory.
5. Configure `openpandect.com`, path `/`, container port `8000` on the web
   Application through Dokploy's native Domains settings. Follow the existing
   Cloudflare ingress setup: internal HTTP, HTTPS and HTTP-to-HTTPS redirection at the
   public edge. Use Dokploy's settings for Traefik. Serve `/events/` through the
   same web route and stream `text/event-stream` without caching or buffering.
   Keep the worker and database private.
6. Deploy the singleton worker first. Its startup applies migrations, emits
   `PANDECT_WORKER_PREPARED <APP_COMMIT_SHA>`, and starts `db_worker`. Migration
   failure stops startup before the marker or worker. Confirm the new worker's
   running task, image digest, and release marker. Deploy web and check the
   expected commit on both `/health/` and `/events/health/`. The latter becomes
   ready once the web process subscribes to PostgreSQL notifications.

Each ASGI web worker maintains one PostgreSQL LISTEN connection and admits up to
64 simultaneous case streams. Excess connections and interrupted streams fall
back to the activity endpoint. If investigators see delayed updates, check
`/events/health/`, then confirm the proxy flushes event responses and forwards
the session cookie on the same origin. Event delivery uses normal case access.

Disable native push autodeployment; GitHub CD owns activation after CI. Application
releases never recreate the independently provisioned database. The singleton,
stop-first worker owns migrations; never migrate concurrently elsewhere. Migrations
must support the running web release; plan destructive changes separately.

Closed registration blocks new invitees from registering. Configure SMTP
before account verification, password resets or invitations. When enabling
[external AI](analysis.md#external-ai-configuration), give web and worker
matching provider settings.

Set `DJANGO_TRUST_PROXY_HEADERS=true` only where the controlled ingress supplies
`X-Forwarded-Proto`. The example enables it and sets one-day HSTS. Django emits
HSTS on requests it sees as secure. `DJANGO_SECURE_SSL_REDIRECT` defaults off
because the edge redirects. Verify redirects, HSTS and sign-in CSRF on the
actual domain.

`PANDECT_TRUSTED_PROXY_COUNT=0` uses the connected address for allauth rate
limits. Set a hop count only for a fixed, trusted chain that supplies genuine
client addresses and blocks direct origin access. Test distinct clients and
forged forwarding headers. Authentication rate limits share Django's PostgreSQL
cache, created by normal account migrations. Staff, model permissions and
current case access govern administration.

After activation, run in the deployed web Application terminal:

```sh
cd /app
python manage.py check --deploy
python manage.py migrate --check
python manage.py check_task_worker --timeout 30
```

`/health/` checks PostgreSQL and reports the build commit; the worker diagnostic
checks queue execution with one harmless probe. Never use that probe as a recurring
health check. Verify running-task digests/commit, marketing pages (`/`, `/evidence/`,
`/trust/`, `/roadmap/`), the `/product/` redirect, static assets, and
closed signup on the live deployment.

Back up the managed database and evidence volume together with writers stopped;
retain private off-host copies. A database dump alone is incomplete. Preserve
resource identities, volume names, server, and placement across redeploys. Local
Compose backup commands do not apply. Review rollback/restoration separately:
older images may not support newer schemas.

### Continuous deployment

The [CD workflow](../.github/workflows/cd.yml) accepts successful CI runs
from upstream `main` pushes. Its secret-free preflight verifies the triggering
CI attempt and its tested commit. A confirmed documentation-only run skips
publication and deployment. See [CI classification](validation.md#documentation-only-runs).

For application runs, native Linux AMD64 and ARM64 builds publish
`build-<CI run ID>-amd64` and `build-<CI run ID>-arm64`. CD combines them under
`build-<CI run ID>`, verifies both platforms and records the immutable digest.
Both Applications run that digest. Versions and source commits are embedded
in the image and OCI labels, and appear as `version` and `commit` on
`/health/`. Local builds default to version `dev`, and use `unknown` when
no source commit is supplied.

Under GitHub **Settings → Environments → production**, set **Deployment branches
and tags** to **Selected branches and tags** and allow only the protected
`main` branch. Store the following values as **environment secrets** in
`production` and remove any repository-level duplicates. Verify a test workflow
on an unapproved branch cannot use the environment before supplying credentials.
See [GitHub's deployment branch and environment settings](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments).

| Secret | Value |
| --- | --- |
| `DOKPLOY_URL` | HTTPS Dokploy origin, without `/api`. |
| `DOKPLOY_API_KEY` | Operator API key permitted to update/deploy these applications and read deployment/task status. |
| `DOKPLOY_WEB_APPLICATION_ID` | Existing native web Application ID. |
| `DOKPLOY_WORKER_APPLICATION_ID` | Existing native worker Application ID. |
| `DOKPLOY_REGISTRY_PASSWORD` | Registry credential with read access to the private GHCR package. |

The registry username is the repository owner. CD publishes with its scoped GitHub
token; Dokploy's long-lived pull credential stays in secrets and Docker provider
configuration. The helper preserves operator-owned environments, mounts, domains,
database, and secret-store settings; it never redeploys PostgreSQL.

CD queues releases without cancelling active ones. Commit ancestry guards
prevent an older source from replacing a newer attempted rollout. Confirm the
[fresh database baseline](architecture.md#primary-keys-and-the-initial-schema)
before setting the `production` environment variable
`PANDECT_PRODUCTION_DATABASE_BASELINE=domain-apps-v1`. The deployment helper
requires this value before mutating Dokploy.

The helper records each rollout as an explicit GitHub deployment, tied to the
tested commit, image digest, CI run and candidate version. The newest record
informs future CI classification. It stays uncertain until both services pass
verification and the helper records success.

The singleton worker updates stop-first, migrates and emits its release marker.
Its Swarm task must stay on the expected image for 60 seconds before web
updates start-first. CD checks web task stability and the expected commit on
both public health endpoints. Run the worker probe separately to verify queue
execution.

Successful GitHub releases use consecutive `v1.N` versions beginning at `v1.0`.
After deployment, CD promotes the verified digest to the candidate version tag
and publishes a GitHub release pointing to the exact tested commit. Release
publication consumes the version number. Interrupted publication can leave
the promoted image in place. Inspect the current deployment, tag and release,
then use **Re-run failed jobs** for a failed publication after a successful
rollout. A matching Git tag can be reused. Conflicts, drafts and sequence gaps
require operator review. The helper leaves existing releases and tags intact.

If a deployment times out, inspect the native task and deployment status
before retrying. It may still be running. Re-running all jobs repeats the
eligibility checks.

## Upload limits and request handling

Uploads stream one file per request, with a 1 GiB per-file limit. The browser
queues up to 100 sequential requests; captured text, manually added messages and
supplied transcripts allow 10 MiB UTF-8. These application limits are defined in
`config/settings.py`, not environment variables. Django's memory threshold controls
temporary storage, not file size. Call audio uses normal file limits/private
storage; transcription is a separate [analysis operation](analysis.md).

Multipart file parts require an active authorized account before temporary
storage. Product uploads check account access before CSRF parses the body and
current owner/editor case access before accepting a file part; anonymous,
unverified, viewer, unrelated, and revoked requests are rejected before file
bytes are written. In admin, adding a file requires staff access, permission to
add that evidence type, and an editable case. The form validates the selected
case before saving. The ASGI handler limits both the declared length and the
bytes received. Budget temporary disk for each concurrent authorized upload because
the raw multipart body and Django's parsed upload can coexist while the original
is saved. Private downloads stream in bounded chunks. Configure request size and
concurrency limits at ingress as well, since proxies can buffer requests.

Use the [vehicle CSV](vehicle-location-imports.md) and
[message and call CSV](evidence.md) guides for specialized intake limits and
review. Individual analysis methods apply their own parser bounds.
Back up staged rows, interpretations and provenance with the database and
private files.

Configure the serving proxy/CDN to limit the entire request body as well. With the
1 GiB file limit, use a 1,026 MiB request cap to leave room for ordinary metadata
and multipart overhead. Providers with smaller request caps require an ingress
that supports the intended upload size. Keep request and application limits aligned.
Ensure upstream request timeouts permit a bounded upload and original save. The
image's Gunicorn timeout is 180 seconds, matching the enhanced upload request
window; slow or interrupted transfers may need retry, not automatic resume.
Provide a safe 413 response at the proxy. Unknown responses retain a frozen
submission ID so retry can reconcile a committed record without creating another.

## Invitations, mail, and private logs

Set `PANDECT_PUBLIC_ORIGIN` to the external HTTP(S) origin, without path/credentials;
invitation email uses it instead of the Host header. Invitations require verified
recipient email and expire after seven days. After
delivery failure, fix SMTP before owner-initiated resend. Sign-in resumes invitations
through the session, without tokens in login URLs.

Configure [SMTP settings](development.md#configuration) before sending mail.
Local debug console mail prints private verification and invitation links.
Keep those logs private.

Case, account, and invitation responses disable caching and use
`Referrer-Policy: same-origin`. This preserves native same-origin form CSRF
validation while suppressing referrers to other origins. Explicit external source
links additionally use `noreferrer`. Do not cache authenticated case material at
a reverse proxy or CDN. Preserve `config.privacy.PrivateAccessLogger` when
overriding the Gunicorn command. Gunicorn and application log filters strip
query strings and redact bearer tokens in invitation, email confirmation, and
password reset URLs. Configure equivalent redaction of request targets and
referrers at the ingress, CDN, and log collector for these route families:

- `/invitations/<token>/`
- `/accounts/confirm-email/<key>/`
- `/accounts/password/reset/key/<uid>-<key>/`

Strip query strings from case search and suggestion URLs. Their `q` values
can contain names, identifiers and evidence text. Keep evidence bodies,
passwords, cookies and bearer tokens out of telemetry.

The optional [request activity log](request-activity.md) has its own private
spool, database, and operator permissions. Follow its setup and recovery guide
when enabling it on a hosted web service.

Reference: [Django deployment checklist](https://docs.djangoproject.com/en/6.1/howto/deployment/checklist/).
