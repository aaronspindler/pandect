# Compose operation and recovery

Complete [local setup](development.md) first. Compose binds application and
database ports to loopback. Its development server, console email, and debug
configuration are for local use.

```sh
docker compose up --build --wait
docker compose ps
docker compose logs --tail 100 web worker db
docker compose exec web python manage.py check_task_worker --timeout 30
docker compose stop
docker compose up --wait
docker compose down
```

`stop` and `down` retain named volumes. `postgres_data` holds PostgreSQL;
`app_data` holds private evidence originals. Do not use `down --volumes` on data
you want to retain. Existing legacy runtime files are not mounted or migrated.

On first run the database must become healthy before the application applies
migrations. On subsequent starts Django applies only pending migrations. The
application health check calls `/health/`, which checks PostgreSQL. If startup
fails, inspect logs, verify environment variables, correct the problem, and rerun
`docker compose up --build --wait`. Changed database passwords in `.env` do not
change passwords already stored in an initialized PostgreSQL volume; rotate them
with PostgreSQL before updating configuration.

The background worker starts after web readiness and uses the same application
image, configuration, and private evidence volume. Its process restarts unless
explicitly stopped. HTTP readiness does not prove queue execution; use the
diagnostic above. Worker failures, manual recovery, and result pruning are
documented in [background tasks](background-tasks.md).

## Database backup and restoration

Keep coordinated database and private-file backups in encrypted off-host storage.
The database includes accounts, case and evidence metadata, captured text, and
private task arguments and results. The `app_data` volume holds original and
derived files. Retain the matching application revision and PostgreSQL major
version with the snapshot.

### Capture a matching backup

Run this from the checkout used by the running local Compose project. Stop any
additional writers, such as a host-based web server or worker, before starting.
The commands stage each attempt in a new owner-only directory. Use a backup
only after both archive checks succeed and the `COMPLETE` marker exists.

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

A capture or archive check failure leaves an incomplete directory and stops the
sequence before restarting writers. Inspect the failure, then retry with a new
snapshot or restart the original project with `docker compose up --wait web
worker`. Copy completed snapshots off-host through your private backup process.
Archive checks inspect the snapshot structure. Test restoration to verify recovery.

### Restore in a separate Compose project

Use a trusted completed snapshot and a checkout matching its application revision
and PostgreSQL major version. Prepare a separate owner-only recovery environment
file using the [local setup guide](development.md), with fresh secrets, closed
signups, and optional external services unset. Start from a shell without
production Django, PostgreSQL, Pandect, or Compose overrides. Use a new project
name so recovery has fresh database and file volumes.

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

Both restores must succeed before web starts. The recovery project uses random
loopback ports. Verify expected records and compare recovered originals against
their retained checksums. For a failed restore, inspect the isolated project
and retry in a fresh one. Keep the original project and volumes available until
the recovered copy is qualified. Stop the recovery project by repeating its
Compose flags with `stop`.

Before enabling a worker or putting a recovered application back into use,
inspect queued and running tasks for possible replay of earlier external
effects. Review [background recovery](background-tasks.md) and run the worker
diagnostic after startup. With external storage, use its coordinated snapshot
and restore procedure and preserve the original storage keys. See [evidence
lifecycle and failed cleanup recovery](evidence.md#private-storage-and-deletion).

## Local image text extraction

The application image includes Tesseract and English data. Host workers need the
[development packages](development.md#run-python-on-the-host). OCR requires no AI
credentials or remote fallback; supported profiles and version recovery are in
[extraction](analysis-extraction.md).

## Upgrades and hosted deployment

Back up before upgrades. After reviewing dependency/runtime compatibility:

```sh
docker compose stop web worker
docker compose build web
docker compose run --rm web python manage.py migrate --noinput
docker compose up --wait
```

Gunicorn serves the application and WhiteNoise serves public assets collected
at image build time. Rebuild after template/static changes and deploy templates
with their matching hashed-asset manifest. Host production installations must run
`python manage.py collectstatic --noinput` before serving.
Private evidence remains separate from collected static files and must never be
published by the proxy or a public storage bucket.

The default `compose.yaml` remains a local development configuration. Use the
[Dokploy recipe](#first-dokploy-deployment) for hosted operation. Other hosted
deployments must explicitly configure HTTPS, allowed hosts,
secure secret storage, SMTP, a matching `PANDECT_PUBLIC_ORIGIN`, private persistent
storage, database backups, and a separate migration step before starting web or
worker replicas. Debug is disabled by default; session/CSRF cookies require HTTPS
in that mode.
Run `python manage.py check --deploy` with the actual deployment configuration and
resolve its findings. Trusted proxy headers must only be configured for a
controlled reverse proxy. Installations predating the domain-app reorganization
require the [fresh baseline](architecture.md#primary-keys-and-the-initial-schema),
not an additive migration or faked history. Retain their old database and media.

## First Dokploy deployment

CI builds versioned GHCR images, CD resolves immutable digests, and Dokploy runs
separate native Docker Applications for **web** and **worker**, plus a managed
**PostgreSQL** resource.
The local `compose.yaml` remains a development tool; it is not used in Dokploy.

1. In the production project, create a managed PostgreSQL resource using
   `postgres:18.6`, with a new database, role, and strong password.
   Keep it private; use its internal hostname and port 5432 in the application
   configuration. Retain Dokploy's database volume and backup ownership.
2. Create native Docker Applications named `web` and `worker` on the same intended
   deployment server. Configure one replica each. Use the `web` and `worker`
   Dockerfile targets' respective GHCR images by digest, never `latest`.
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

Keep native push autodeployment disabled: the GitHub CD workflow owns activation
after successful CI. The database is provisioned independently and is not
recreated on application releases. The worker's stop-first, one-replica rollout
keeps migration ownership singular. Use additive migrations compatible with the
currently running web release; destructive schema changes need a separately
planned rollout. Never run migrations concurrently from another process.

Registration is rejected on both GET and POST, including new invited accounts;
existing accounts can still sign in. No AI runtime or inference credentials are
needed for this first deployment. Leave every inference URL and token unset.
SMTP may remain unconfigured for the marketing launch; password resets, email
verification, and invitation delivery require working SMTP before use.

The deployment trusts `X-Forwarded-Proto` only behind the existing controlled
proxy and sets the exact HTTPS CSRF origin. Secure session/CSRF cookies remain
enabled. The edge owns HTTPS redirection. Verify public HTTP redirects, HTTPS
sign-in CSRF, and proxy behavior on the actual domain. Assess Django's deployment
check findings against the edge configuration and the one-day HSTS policy.

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

`/health/` checks PostgreSQL and reports the image's build commit. It does not
prove queue execution: the worker diagnostic enqueues one harmless probe. Do not
use that diagnostic as a recurring container health check. Verify the expected
image digests and commit on the actual running tasks, the public marketing routes
(`/`, `/product/`, `/trust/`, `/hosting/`), their static assets, and the closed
signup page. Local tests do not establish live service health.

Back up the managed database and shared private evidence volume together, with
application writers stopped, and retain private off-host copies. A database dump
alone is incomplete. Keep native resource identities, volume names, server, and
placement stable across redeploys. Do not use the local Compose backup commands
against Dokploy resources. An older image may not support a newer schema;
rollback or matching backup restoration is a separately reviewed recovery action.

### Continuous deployment

After all CI checks pass on a push to Pandect's upstream `main`, CI builds
the Dockerfile's `web` and `worker` targets for Linux amd64 and publishes private
`ghcr.io/<owner>/<repository>-web` and `-worker` images. Each has the commit tag
and a `build-<CI run ID>` tag; the build summary records its immutable digest.
Images embed `APP_COMMIT_SHA` so live health identifies their source revision.
Pull requests and manually dispatched CI do not publish or activate releases.

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
| `DOKPLOY_REGISTRY_PASSWORD` | Registry credential with read access to both private GHCR packages. |

The registry username is the repository owner. CI uses its scoped GitHub token
for package publication; the long-lived Dokploy pull credential stays in secrets
and the Docker provider configuration. Application environment, mounts, domain,
database, and secret-store configuration remain under their operator owners.
The deployment helper does not replace those settings or redeploy PostgreSQL.

The separate CD workflow activates only successful `CI` runs from same-repository
pushes to `main`. It checks the current `main` commit and skips stale results.
For the [domain-app migration baseline](architecture.md#primary-keys-and-the-initial-schema),
retain the previous database and evidence volume, prepare a fresh PostgreSQL
database with an empty queue, and configure both Dokploy Applications to use it.
Once both are configured, set the GitHub
**production** environment variable `PANDECT_PRODUCTION_DATABASE_BASELINE` to
`domain-apps-v1`. The deployment helper stops before changing either Application
until this setting is present. Rerun the failed **Deploy production** workflow
for the successful CI run on current `main` once the database is ready.

For each image, both the successful run's tag and commit tag must resolve to the
same digest; Dokploy receives `repository@sha256:...`, never a moving tag. CD
checks out only the trusted `main` deployment helper and executes no downloaded
CI artifacts. Its production concurrency group waits for the active rollout
instead of cancelling it when a newer commit arrives.

CD updates each native Application's Docker provider and triggers deployment,
preserving runtime environment and mounts. Both Applications remain singleton;
the worker uses stop-first updates and web uses start-first updates. The worker
deploys first, applies migrations, and must
report the exact prepared marker while one running Swarm task uses the expected
image for a 60-second stability window. Then web deploys and must report the
expected commit over `https://openpandect.com/health/`, with a stable running task
on its exact image. An accepted deploy request alone does not count as success.
Queue execution still requires the bounded `check_task_worker` diagnostic above;
CD's worker checks establish startup and task identity.

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

Requests and readiness waits are bounded. The helper rejects HTTP redirects and
leaves failed mutations for operator review. A readiness timeout identifies the
service and the check it was waiting on. Inspect the native deployment/task
status before rerunning because the remote rollout can continue after a timeout.
The helper refuses another deployment when it detects an active one. A worker
or web failure stops the remaining steps and can leave a partial release
requiring operator review. Keep migrations compatible with the running release
and take backups before schema upgrades.

## Upload limits and request handling

The application streams one file per request, bounded by `PANDECT_MAX_FILE_BYTES`.
The browser queues up to `PANDECT_MAX_BATCH_FILES` sequential requests;
`PANDECT_MAX_TEXT_BYTES` bounds captured text, messages, and call transcripts.
Django's upload memory threshold controls temporary storage, not maximum file size.
Call audio uses the normal file limit/private storage. Uploading it does not
request transcription; inference is a separate [analysis operation](analysis.md).

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

Set `PANDECT_PUBLIC_ORIGIN` to the externally visible HTTP(S) origin, without a
path or credentials. It is used in invitation emails rather than trusting an
incoming Host header. Invitations require verified recipient email and expire
after `PANDECT_INVITATION_DAYS` (default seven). Failed delivery leaves an
invitation available for owner-initiated resend; correct SMTP settings first.
Sign-in resumes the invitation through the session without carrying its token
in the login URL.

Production SMTP uses the configured sender, STARTTLS, and bounded timeout. Console
mail is a development-only feature and deliberately prints verification/invitation
links; keep those local logs private and do not enable debug for hosted operation.

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
