# Compose operation and recovery

Use [local development](development.md#first-run) for setup and startup. Compose
binds application and database ports to loopback. Its development server, console
email, and debug configuration are for local use.

```sh
docker compose ps
docker compose logs --tail 100 web worker db
docker compose stop
docker compose down
```

`stop` and `down` retain `postgres_data` (PostgreSQL) and `app_data` (private
evidence). Never use `down --volumes` on retained data. Legacy runtime files are
not mounted or migrated.

The application migrates after database readiness; later starts apply only pending
migrations. `/health/` checks PostgreSQL. On startup failure, inspect logs and
environment variables, fix the cause, and rerun `make run`. Changing `.env` does
not rotate passwords in initialized PostgreSQL volumes: rotate in PostgreSQL first.

HTTP readiness does not prove queue execution. Use the [worker diagnostic and
recovery procedures](background-tasks.md#startup-and-diagnostics) after startup.

## Database backup and restoration

Keep coordinated database/private-file backups encrypted off-host, with their
application revision and PostgreSQL major version. The database holds accounts,
case/evidence metadata, captured text, and private task arguments/results;
`app_data` holds originals and derived files.

### Capture a matching backup

Run from the active local Compose checkout after stopping additional writers
(including host web/worker processes). Each attempt uses a new owner-only directory.
Use a backup only after both archive checks pass and `COMPLETE` exists.

```sh
(
  set -eu
  umask 077
  snapshot=$(mktemp -d "$HOME/pandect-backup.XXXXXXXX")
  printf 'Snapshot directory: %s\n' "$snapshot"

  docker compose stop web worker
  docker compose exec -T db sh -c \
    'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$snapshot/database.dump"
  docker compose run --rm --no-deps -T web tar -C /app/data -cf - . \
    > "$snapshot/app-data.tar"

  docker compose exec -T db pg_restore --list < "$snapshot/database.dump" > /dev/null
  tar -tf "$snapshot/app-data.tar" > /dev/null
  touch "$snapshot/COMPLETE"
  docker compose up --wait web worker
)
```

A capture/check failure leaves writers stopped and the snapshot incomplete.
Inspect it, then retry with a new snapshot or restart with
`docker compose up --wait web worker`. Copy completed snapshots off-host.
Archive checks verify structure; test restoration to qualify recovery.

### Restore in a separate Compose project

Use a trusted completed snapshot and matching application revision/PostgreSQL
major version. Follow [local setup](development.md) for a separate owner-only
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
    PANDECT_DB_PORT=0 PANDECT_WEB_PORT=0 \
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

  recovery up --wait db web
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

## Local image text extraction

The application image includes Tesseract and English data. Host workers need the
[development packages](development.md#run-python-on-the-host). OCR requires no AI
credentials or remote fallback; supported profiles and version recovery are in
[extraction](analysis-extraction.md).

## Upgrades and hosted deployment

Back up before upgrades and stop old web writers/workers before migration.
Findings catalogue migrations backfill retained JSON/input snapshots in bounded,
resumable per-result transactions. They do not read originals, rerun analysis,
contact providers, rewrite result/review JSON, or rebuild at startup. Duration
grows with retained findings. These additive migrations require the current
[baseline](architecture.md); they cannot upgrade rewritten pre-release baselines.
The PDF renderer is pinned in the application lockfile.

After reviewing dependency/runtime compatibility:

```sh
docker compose stop web worker
docker compose build web
docker compose run --rm web python manage.py migrate --noinput
docker compose up --wait
```

Gunicorn serves the application; WhiteNoise serves build-collected public assets.
Rebuild after template/static changes and deploy matching templates/hashed-asset
manifests. Host production installs need `python manage.py collectstatic --noinput`
before serving. Never publish private evidence through a proxy or public bucket.

`compose.yaml` is for local development; use the [Dokploy recipe](#first-dokploy-deployment)
for hosting. Other hosts need HTTPS, allowed hosts, secure secrets, SMTP, matching
`PANDECT_PUBLIC_ORIGIN`, private persistent storage, database backups, and migration
before web/worker startup. Debug defaults off, requiring HTTPS session/CSRF cookies.
Run `python manage.py check --deploy` with actual configuration and resolve findings.
Trust proxy headers only behind a controlled proxy. Older installations require a
[fresh baseline](architecture.md#primary-keys-and-the-initial-schema), with their
old database/media retained; do not fake migration history.

## First Dokploy deployment

CI builds one GHCR image; CD resolves its immutable digest. Dokploy runs native
**web** and **worker** Applications plus managed **PostgreSQL**, without Compose.

1. In the production project, create a managed PostgreSQL resource using
   `postgres:18.6`, with a new database, role, and strong password.
   Keep it private; use its internal hostname and port 5432 in the application
   configuration. Retain Dokploy's database volume and backup ownership.
2. Create native Docker Applications named `web` and `worker` on the same intended
   deployment server. Configure one replica each. Use the same
   `ghcr.io/<owner>/<repository>@sha256:<digest>` image for both, never `latest`.
   Leave web's Command and Arguments overrides empty so it uses the image's
   default Gunicorn command. Set worker's Command to `sh` and Arguments to the
   single entry `/app/scripts/docker-entrypoint-worker.sh`.
   Configure the web Application's native HTTP health check against
   `http://127.0.0.1:8000/health/`; keep worker rollout stop-first and web start-first.
3. Copy [`.env.dokploy.example`](../.env.dokploy.example) into both application
   environments, replacing blank secrets and the database hostname. Set
   `DJANGO_DEBUG=false`, `PANDECT_ALLOW_SIGNUPS=false`, and
   `PANDECT_PUBLIC_ORIGIN=https://openpandect.com`. Keep both applications on the
   same secret key and database credentials.
4. Mount the same private persistent named volume at `/app/data` in both
   applications. Keep them on the same server and storage-owning Swarm node;
   local named volumes are not shared between nodes. The application UID must
   own the mount. No public route may serve that directory.
5. Configure `openpandect.com`, path `/`, container port `8000` on the web
   Application through Dokploy's native Domains settings. Follow the existing
   Cloudflare ingress setup: internal HTTP, HTTPS at the public edge. Configure
   the public HTTP-to-HTTPS redirect at that edge. Do not edit Traefik directly.
   Do not create domains or published ports for the worker or database.
6. Deploy the singleton worker first. Its startup applies migrations, emits
   `PANDECT_WORKER_PREPARED <APP_COMMIT_SHA>`, and starts `db_worker`. Migration
   failure stops startup before the marker or worker. Confirm the new worker's
   running task, image digest, and release marker before deploying web. Web
   starts Gunicorn directly and serves build-collected WhiteNoise assets.

Disable native push autodeployment; GitHub CD owns activation after CI. Application
releases never recreate the independently provisioned database. The singleton,
stop-first worker owns migrations; never migrate concurrently elsewhere. Migrations
must support the running web release; plan destructive changes separately.

Closed registration rejects GET/POST, including new invitees; existing accounts
can sign in. Leave inference URLs/tokens unset for this first deployment. SMTP is
optional for the marketing launch but required before password resets,
verification, or invitation delivery.

Trust `X-Forwarded-Proto` only behind the controlled proxy; set the exact HTTPS
CSRF origin and retain secure session/CSRF cookies. Verify edge HTTP-to-HTTPS
redirects, sign-in CSRF, and proxy behavior on the actual domain. Assess Django's
deployment findings against edge configuration and the one-day HSTS policy.

For allauth rate limits, `PANDECT_TRUSTED_PROXY_COUNT=0` uses the directly connected
address and ignores `X-Forwarded-For`. Set the count only when the controlled proxy
chain supplies the genuine client address, its hop count is fixed, and clients
cannot bypass the proxy. Verify distinct clients have separate limits and forged
forwarding headers do not bypass them. Use a shared cache for reliable limits
across multiple web processes. Restrict or rate-limit `/admin/login/` at the edge
because Django admin has its own sign-in flow.

After activation, run in the deployed web Application terminal:

```sh
cd /app
python manage.py check --deploy
python manage.py migrate --check
python manage.py check_task_worker --timeout 30
```

`/health/` checks PostgreSQL and reports the build commit; the worker diagnostic
checks queue execution with one harmless probe. Never use that probe as a recurring
health check. Verify running-task digests/commit, marketing routes (`/`, `/product/`,
`/trust/`, `/hosting/`), static assets, and closed signup on the live deployment.

Back up the managed database and evidence volume together with writers stopped;
retain private off-host copies. A database dump alone is incomplete. Preserve
resource identities, volume names, server, and placement across redeploys. Local
Compose backup commands do not apply. Review rollback/restoration separately:
older images may not support newer schemas.

### Continuous deployment

After checks pass for an upstream `main` push, CI publishes one Linux amd64 image
to private `ghcr.io/<owner>/<repository>`, tagged with the commit and
`build-<CI run ID>`. The summary records its digest; embedded `APP_COMMIT_SHA`
identifies the live revision. PRs and manual CI runs do not publish/activate releases.

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

The registry username is the repository owner. CI publishes with its scoped GitHub
token; Dokploy's long-lived pull credential stays in secrets and Docker provider
configuration. The helper preserves operator-owned environments, mounts, domains,
database, and secret-store settings; it never redeploys PostgreSQL.

CD accepts successful `CI` runs only from same-repository `main` pushes and skips
stale commits. For the [domain-app baseline](architecture.md#primary-keys-and-the-initial-schema),
retain the old database/evidence volume, prepare a fresh PostgreSQL database and
empty queue, and point both Applications to it. Then set **production** environment
variable `PANDECT_PRODUCTION_DATABASE_BASELINE=domain-apps-v1`; the helper refuses
mutations without it. Once ready, rerun **Deploy production** for successful CI
on current `main`.

The run and commit tags must resolve to the same digest. CD supplies it as
`PANDECT_IMAGE`; both Applications receive `repository@sha256:...`. CD uses only the
trusted `main` helper, never downloaded CI artifacts. Production concurrency waits
for active rollouts without cancelling them for newer commits.

CD updates each native Application's Docker provider, sets worker's Command to
`sh` with `/app/scripts/docker-entrypoint-worker.sh` as its sole Argument, and
clears web's Command and Arguments overrides to use the image default. It then
triggers deployment, preserving runtime environment and mounts. Both Applications
remain singleton; the worker uses stop-first updates and web uses start-first
updates. The worker deploys first, applies migrations, and must report the exact
prepared marker while one running Swarm task uses the expected
image for a 60-second stability window. Then web deploys and must report the
expected commit over `https://openpandect.com/health/`, with a stable running task
on its exact image. An accepted deploy request alone does not count as success.
Queue execution still requires the bounded `check_task_worker` diagnostic above;
CD's worker checks establish startup and task identity.

When replacing separate `-web`/`-worker` packages, preserve Application IDs,
environments, database, and shared volume. Check pull access to the shared GHCR
package first. Successful CI/CD switches both images and startup commands without
a database reset or extra migration. Retain old digests/commands for reviewed recovery.

Only after both services pass verification does the deployment helper emit its
`deployed=true` output. A dependent job then publishes a GitHub release tagged
`deploy-<full tested commit SHA>`, pointing to that exact commit, with generated
release notes and links to the CI and deployment runs. Tags identify deployed
commits; semantic versions remain managed separately. Stale CI skips and
failed or partial deployments never enable the release job. The release helper
comes from the trusted workflow revision; only this job receives
`contents: write` through the built-in GitHub token, without Dokploy secrets.

Reruns preserve an existing published release after checking that its tag
resolves to the tested commit. A matching tag left by an interrupted publication
can be reused to create the missing release. Conflicting tags, existing drafts,
and API failures stop publication for operator review; the helper never moves
tags or edits/deletes existing releases. Deployment releases leave GitHub's
**Latest** designation unchanged so a late retry cannot supersede a newer release.
If publication fails after deployment
succeeds, production remains deployed. Use GitHub Actions **Re-run failed jobs**
to retry only publication using the successful deployment's output; rerunning
the entire workflow repeats the deployment and its current-`main` check.
Inspect an uncertain publication before retrying. Repository tag rules must
permit the workflow to create `deploy-*` tags. GitHub can also reject release
creation with HTTP 403/404 if the target commit's workflow files differ from the
current default branch and the token lacks workflow-modification authority;
inspect the target and repository rules rather than adding fallback credentials.
See [GitHub's release API](https://docs.github.com/en/rest/releases/releases#create-a-release).

Requests/waits are bounded; redirects are rejected. Timeouts identify the service
and pending check. Inspect native deployment/task status before retrying: rollout
may continue after timeout, and the helper refuses detected active deployments.
Failures stop remaining steps and may leave partial releases for operator review.
Back up before schema upgrades and preserve compatibility with the running release.

## Upload limits and request handling

Uploads stream one file per request, bounded by `PANDECT_MAX_FILE_BYTES`. The
browser queues up to `PANDECT_MAX_BATCH_FILES` sequential requests;
`PANDECT_MAX_TEXT_BYTES` bounds text, messages, and transcripts. Django's memory
threshold controls temporary storage, not file size. Call audio uses normal file
limits/private storage; transcription is a separate [analysis operation](analysis.md).

Vehicle CSV imports accept up to 100 MiB and 100,000 data rows, respecting any
smaller operator file limit. Worker parsing, paginated review, and atomic
publication are described in [vehicle-location imports](vehicle-location-imports.md).
Back up source documents, staged rows, interpretations, progress, and provenance
with the rest of the database and private files. File cleanup and orphan recovery
follow the [evidence lifecycle](evidence.md#private-storage-and-deletion).

Configure the serving proxy/CDN to limit the entire request body as well. With the
default 100 MiB file limit, use a 102 MiB request cap to leave room for ordinary
metadata and multipart overhead; revise limits together if configuration changes.
Ensure upstream request timeouts permit a bounded upload and original save. The
image's Gunicorn timeout is 180 seconds, matching the enhanced upload request
window; slow or interrupted transfers may need retry, not automatic resume.
Provide a safe 413 response at the proxy. Unknown responses retain a frozen
submission ID so retry can reconcile a committed record without creating another.

## Invitations, mail, and private logs

Set `PANDECT_PUBLIC_ORIGIN` to the external HTTP(S) origin, without path/credentials;
invitation email uses it instead of the Host header. Invitations require verified
recipient email and expire after `PANDECT_INVITATION_DAYS` (default seven). After
delivery failure, fix SMTP before owner-initiated resend. Sign-in resumes invitations
through the session, without tokens in login URLs.

Production SMTP uses the configured sender, STARTTLS, and a bounded timeout.
Development console mail prints private verification/invitation links; keep logs
private and hosted debug disabled.

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

Keep evidence bodies, passwords, cookies, and bearer tokens out of telemetry.
Review access and retention for logs produced before equivalent redaction was in
place.

Reference: [Django deployment checklist](https://docs.djangoproject.com/en/6.1/howto/deployment/checklist/).
