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

Keep backups private and off-host. The database contains accounts, authentication
state, cases, invitations, collaboration memberships, people, vehicles, captured text,
archive attribution, and file evidence metadata.
It also contains queued work and operational task results, including private
arguments, return values, and exception details.
File originals live separately in `app_data`; a database dump alone is incomplete.
Stop application writers while taking matching database and file snapshots. For
the current Compose stack, stop `web` and `worker`, create both backups, then
restart them:

```sh
mkdir -p backups
docker compose stop web worker
docker compose exec -T db sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > backups/pandect.dump
docker compose run --rm --no-deps -T web tar -C /app/data -cf - . \
  > backups/pandect-files.tar
docker compose up --wait web worker
```

Test restoration into a **new** database, preserving the original:

```sh
docker compose exec -T db sh -c \
  'createdb -U "$POSTGRES_USER" pandect_restore'
docker compose exec -T db sh -c \
  'pg_restore --exit-on-error --no-owner -U "$POSTGRES_USER" -d pandect_restore' \
  < backups/pandect.dump
docker compose exec -T db sh -c \
  'psql -U "$POSTGRES_USER" -d pandect_restore -c "SELECT count(*) FROM accounts_user"'
```

For actual recovery, stop `web` and `worker`, set `POSTGRES_DB=pandect_restore` in
`.env`, and start only `db web` with `docker compose up --wait db web`.
Keep the original database until the restored application and expected data
have been checked. Do not restore an
untrusted dump or change PostgreSQL major versions by replacing a container tag.
Before starting a worker against a restored database, inspect queued/running work:
restoration can replay pending work whose external effects already happened.
Review the task store and feature-specific recovery needs, then start `worker`
and run its diagnostic. Do not blindly replay running tasks.

When recovering evidence, restore the matching file snapshot into a separate
private storage location and configure the recovered application to use it before
serving requests. For Compose, use a separate recovery project's `app_data` volume;
do not unpack a snapshot over the original volume. Keep storage keys unchanged.
For an external storage backend, use its coordinated snapshot/restore procedure.
Verify original bytes against stored checksums after restoration. See
[evidence lifecycle and failed cleanup recovery](evidence.md#private-storage-and-deletion).

## Local image text extraction

Rebuild the application image after upgrading to local OCR. The web/worker image
includes Tesseract and English language data. Host-based workers must install
these system packages as described in [development](development.md#run-python-on-the-host).
No AI credentials are required for image OCR. `PANDECT_AI_VISION_MODEL` is retired
and ignored; there is no remote fallback. Old OCR results stay readable; queued
older method versions need a new run. Do not reset or delete their history.

## Upgrades and hosted deployment

Back up before upgrades. After reviewing dependency/runtime compatibility:

```sh
docker compose stop web worker
docker compose build web
docker compose run --rm web python manage.py migrate --noinput
docker compose up --wait
```

The image's default Gunicorn entry point serves the application and public
application assets through WhiteNoise. `collectstatic` runs at image build time;
rebuild the image when templates/static assets change. A host-based production
installation must run `python manage.py collectstatic --noinput` before serving.
Non-debug pages reference content-hashed asset filenames so a browser cannot reuse
an older stylesheet or script for a new release. Deploy the templates and their
collected manifest together. Debug mode also uses WhiteNoise, with immediate
cache revalidation for source assets.
Private evidence remains separate from collected static files and must never be
published by the proxy or a public storage bucket.

The default `compose.yaml` remains a local development configuration. Use the
[Dokploy recipe](#first-dokploy-deployment) for hosted operation. Other hosted
deployments must explicitly configure HTTPS, allowed hosts,
secure secret storage, SMTP, a matching `PANDECT_PUBLIC_ORIGIN`, private persistent
storage, database backups, and a separate migration step before starting web or
worker replicas. Queue tables are added by the dependency's migrations; no
Pandect domain migration or data reset is needed for the worker foundation.
Debug is disabled by default; session/CSRF cookies require HTTPS in that mode.
Run `python manage.py check --deploy` with the actual deployment configuration and
resolve its findings. Trusted proxy headers must only be configured for a
controlled reverse proxy. This pre-release app reorganization is an exception to ordinary additive
upgrades: provision a fresh database and empty queue, migrate them, and configure
both web and worker to use that new baseline. Do not apply the rewritten initial
migrations to an old database or fake its history. See the
[fresh baseline](architecture.md#primary-keys-and-the-initial-schema). Runtime
database/media deletion is a separate deliberate operator action.

## First Dokploy deployment

Use the same deployment shape as `spindlers-monorepo`: CI builds versioned GHCR
images, CD resolves immutable digests, and Dokploy runs separate native Docker
Applications for **web** and **worker**, plus a managed **PostgreSQL** resource.
The local `compose.yaml` remains a development tool; it is not used in Dokploy.

1. In the existing `openpandect` production project, create a managed PostgreSQL
   resource using `postgres:18.6`, with a new database, role, and strong password.
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
enabled. As in the monorepo, application HTTPS redirection is disabled because
the edge owns it. Verify public HTTP redirects, HTTPS sign-in CSRF, and current
proxy behavior on the actual domain. Do not broaden shared proxy trust to make a
local check pass. One-day HSTS does not include subdomains or preload. Deployment
checks can report the deliberately external SSL redirect and those optional
HSTS policies; assess them against the actual edge configuration.

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

The GitHub Actions workflow follows the monorepo's GHCR and native Dokploy Docker
provider pattern. After all existing CI checks pass on a push to `main`, CI builds
the Dockerfile's `web` and `worker` targets for Linux amd64 and publishes private
`ghcr.io/<owner>/<repository>-web` and `-worker` images. Each has the commit tag
and a `build-<CI run ID>` tag; the build summary records its immutable digest.
Images embed `APP_COMMIT_SHA` so live health identifies their source revision.
Pull requests and manually dispatched CI do not publish or activate releases.

Configure these secrets in the GitHub **production** environment, using the same
names as the monorepo where applicable:

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
database, and Doppler configuration remain under their existing operator owners.
The deployment helper does not replace those settings or redeploy PostgreSQL.

The separate CD workflow activates only successful `CI` runs from same-repository
pushes to `main`. It checks the current `main` commit and skips stale results.
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

Requests and readiness waits are bounded. Mutation failures are not retried
automatically, and a timeout does not cancel a remote deployment. Inspect the
native deployment/task status before rerunning; the helper refuses another
observably active deployment. A worker or web failure stops the remaining steps
and can leave a partial release requiring operator review. Keep migrations
compatible with the running release and take backups before schema upgrades.

## Upload limits and request handling

The application streams one file per request and enforces
`PANDECT_MAX_FILE_BYTES` before retaining an original. The browser may queue up to
`PANDECT_MAX_BATCH_FILES`, sending entries sequentially; it does not submit one
large multipart batch. `PANDECT_MAX_TEXT_BYTES` bounds captured UTF-8 text,
including message bodies and supplied call transcripts. Phone-call and FaceTime
recordings use the same per-file limit and private storage as other originals.
Backups must include call/attachment rows, transcript text, and the corresponding
private audio files. Attachments follow the existing after-commit deletion
cleanup and manual orphan-reconciliation procedures; rollback does not remove
retained files. Transcription is separately selected case analysis, defaulting to
a private runtime; the ordinary upload path does not submit recordings for AI.
Optional inference services and model-file backup/recovery are described in
[local analysis](local-analysis.md). They use an isolated internal network, pinned
native engines and operator-provisioned read-only models. External AI remains
separately selected and approved through Cloudflare.
Django's upload memory threshold controls temporary storage, not maximum file size.
Asset Location CSV imports accept up to 100 MiB and 100,000 data rows,
respecting a smaller operator file limit. Their route streams with the smaller
byte limit. Parsing and publication run in the existing worker with bounded
chunks and a paginated review; publication commits all observations together.
Original CSV documents, import interpretations, staged review rows, progress,
and source-row provenance must be included in coordinated
database/private-file backups. See [vehicle-location imports](vehicle-location-imports.md).

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
a reverse proxy or CDN. The default image uses
`config.privacy.PrivateAccessLogger` to redact invitation, email confirmation,
and password reset keys, along with query strings in request and referrer URLs.
Application and development log filters apply the same URL redaction. Preserve
that logger when overriding the image command. Configure equivalent redaction at
proxies, CDNs, and log collectors that receive requests before the application.
Never add evidence bodies, passwords, cookies, or invitation tokens to telemetry.

Reference: [Django deployment checklist](https://docs.djangoproject.com/en/6.1/howto/deployment/checklist/).
