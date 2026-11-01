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
Private evidence remains separate from collected static files and must never be
published by the proxy or a public storage bucket.

Compose remains a local development configuration, not a hosted deployment
recipe. A hosted deployment must explicitly configure HTTPS, allowed hosts,
secure secret storage, SMTP, a matching `PANDECT_PUBLIC_ORIGIN`, private persistent
storage, database backups, and a separate migration step before starting web or
worker replicas. Queue tables are added by the dependency's migrations; no
Pandect domain migration or data reset is needed for the worker foundation.
Debug is disabled by default; session/CSRF cookies require HTTPS in that mode.
Run `python manage.py check --deploy` with the actual deployment configuration and
resolve its findings. Trusted proxy headers must only be configured for a
controlled reverse proxy. Interface upgrades use additive `0002` migrations;
never reset a database as an incidental upgrade step.

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

Production SMTP uses the configured sender, STARTTLS, and bounded timeout. Console
mail is a development-only feature and deliberately prints verification/invitation
links; keep those local logs private and do not enable debug for hosted operation.

Case, account, and invitation responses disable caching and use
`Referrer-Policy: same-origin`. This preserves native same-origin form CSRF
validation while suppressing referrers to other origins. Explicit external source
links additionally use `noreferrer`. Do not cache authenticated case material at
a reverse proxy or CDN. The default image uses
`config.privacy.PrivateAccessLogger` to redact invitation token path segments
and query strings, including referrer URL values, from Gunicorn access logs;
application/development log filters apply matching URL redaction. Preserve that logger when overriding the image
command. Configure the same path/query/referrer redaction at external proxies/CDNs
and any logging collector; the application cannot sanitize logs produced before requests reach it.
Never add evidence bodies, passwords, cookies, or invitation tokens to telemetry.

Reference: [Django deployment checklist](https://docs.djangoproject.com/en/6.1/howto/deployment/checklist/).
