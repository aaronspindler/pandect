# Compose operation and recovery

Complete [local setup](development.md) first. Compose binds application and
database ports to loopback. Its development server, console email, and debug
configuration are for local use.

```sh
docker compose up --build --wait
docker compose ps
docker compose logs --tail 100 web db
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

## Database backup and restoration

Keep backups private and off-host. The database contains accounts, authentication
state, cases, collaboration memberships, text evidence, and file evidence metadata.
File originals live separately in `app_data`; a database dump alone is incomplete.
Stop application writers while taking matching database and file snapshots. For
the current Compose stack, stop `web`, create both backups, then restart it:

```sh
mkdir -p backups
docker compose stop web
docker compose exec -T db sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > backups/pandect.dump
docker compose run --rm --no-deps -T web tar -C /app/data -cf - . \
  > backups/pandect-files.tar
docker compose up --wait web
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

For actual recovery, stop `web`, set `POSTGRES_DB=pandect_restore` in `.env`, and
start the stack with `docker compose up --wait`. Keep the original database until
the restored application and expected data have been checked. Do not restore an
untrusted dump or change PostgreSQL major versions by replacing a container tag.

When recovering evidence, restore the matching file snapshot into a separate
private storage location and configure the recovered application to use it before
serving requests. For Compose, use a separate recovery project's `app_data` volume;
do not unpack a snapshot over the original volume. Keep storage keys unchanged.
For an external storage backend, use its coordinated snapshot/restore procedure.
Verify original bytes against stored checksums after restoration. See
[evidence lifecycle and failed cleanup recovery](evidence.md#private-storage-and-deletion).

## Upgrades and hosted deployment

Back up before upgrades. After reviewing dependency/runtime compatibility:

```sh
docker compose stop web
docker compose build web
docker compose run --rm web python manage.py migrate --noinput
docker compose up --wait
```

The image's default Gunicorn entry point is available for future deployment work,
but the Compose configuration is not a hosted deployment recipe. A hosted
deployment must explicitly configure HTTPS, allowed hosts, secure secret storage,
SMTP, static-file serving (`python manage.py collectstatic --noinput`), private
persistent storage, database backups, and a separate migration step before
starting application replicas. Debug is disabled by default, and session/CSRF
cookies require HTTPS in that mode. Run `python manage.py check --deploy` with the
actual deployment configuration and resolve its findings. Trusted proxy headers
must only be configured for a controlled reverse proxy.

Reference: [Django deployment checklist](https://docs.djangoproject.com/en/6.1/howto/deployment/checklist/).
