# Request activity

Request activity is an optional private origin log for site operators.
Gunicorn records requests for the configured public host after handling them,
including static files and signed-in routes. Live event requests appear when
their streams close. Responses served solely at Cloudflare fall outside its
coverage.

Records include time, method, server-reported status, route label, verified
client IP and a versioned fingerprint when available. A dedicated key hashes
normalized User-Agent and Accept-Language values. Similar browsers can share a
fingerprint, and browser updates or key rotation can change it. Storage excludes
request and response content, raw targets, header strings, cookies and account
or case IDs.

## Review requests

With capture enabled, active staff in **Request activity operators** can open
the read-only **Request activity** view in administration. Search retained
history by IP or complete versioned fingerprint, optionally with UTC dates.
Retention has no scheduled expiry.

The 30-day summary estimates returning browser configurations using eligible
public HTML requests on at least two UTC dates. It counts fingerprint versions
separately, shows missing signals, and can include automated requests.

## Set up the hosted service

Collection defaults to off. For the official deployment, verify the actual proxy
chain and block direct access to the origin from untrusted clients. The last
trusted hop must deliver a single `CF-Connecting-IP` value from Cloudflare and
prevent clients from supplying their own value. Verify using synthetic requests
through the edge and through any alternate route to the origin. Confirm that
the chosen retention policy fits the deployment's privacy obligations and explain
the collection in the public notice before enabling it.

Run the standalone management commands with a nonempty `DJANGO_SECRET_KEY`.
They can use their own value and run independently of the case database.

1. Provision a separate private PostgreSQL database for request activity. Use
   separate schema owner, writer, and read-only reader credentials. Initialize
   its schema with `python manage.py request_activity init` using
   `PANDECT_REQUEST_ACTIVITY_MIGRATION_DSN`. Keep this credential out of the
   running web and ingestion services. Grant the runtime roles `CONNECT` on
   this database and `USAGE` on the `request_activity` schema. Grant the writer
   `INSERT` and the reader `SELECT` on `request_activity.events`. The runtime
   roles need no access to the case database or schema ownership. Keep the
   telemetry database's statement and error logs private and verify their
   parameter handling with synthetic requests.
2. Mount a private persistent local volume into both the web and ingestion
   containers on the same storage-owning node. Make the spool directory owned
   by the Pandect UID with mode `0700`, on storage with a quota independent of
   the case and database volumes. Set `PANDECT_REQUEST_ACTIVITY_SPOOL` to the
   same absolute SQLite file path in both containers.
3. Run `python manage.py request_activity ingest` as a separate supervised
   service using the release image, spool volume, and
   `PANDECT_REQUEST_ACTIVITY_WRITER_DSN`. The `--once` option drains a single
   batch for diagnostics. The ingester can drain already captured records while
   web capture is disabled.
4. On the web service, configure `PANDECT_REQUEST_ACTIVITY_READER_DSN`,
   `PANDECT_REQUEST_ACTIVITY_TRUSTED_PROXIES` as a comma-separated list of exact
   ingress addresses or CIDRs, and a stable, dedicated
   `PANDECT_REQUEST_ACTIVITY_KEY` of at least 32 bytes. Set a new
   `PANDECT_REQUEST_ACTIVITY_KEY_VERSION` when replacing that key. Keep key and
   DSN values in private service configuration. Confirm the intended
   `PANDECT_PUBLIC_ORIGIN`, then opt in with `PANDECT_REQUEST_ACTIVITY_ENABLED=true`.
   Keep the release's Gunicorn ASGI configuration and access hook enabled.
   With capture on, the private request records replace Gunicorn's per-request
   stdout lines. Capture warnings continue to use the operational log.
5. Use synthetic canaries to verify capture, the approved fields in the private
   store, the absence of sensitive values from ingress, Gunicorn and collector
   logs, and access through the intended admin accounts. Repeat after changes to
   the proxy chain or access log configuration.

Run `python manage.py request_activity check` every 30 seconds and alert on
nonzero exit. It reports absent or stale heartbeats, records buffered over 60
seconds and capture gaps within five minutes, without exposing request fields.

## Recover and roll back

A background writer moves events from Gunicorn's 512-record memory queue to
a SQLite spool. The ingester acknowledges each batch after PostgreSQL commits,
so it can replay safely. A crash before spooling or a full buffer can lose
records. Check capture health and known gaps in administration when interpreting
missing requests. The spool's main file is capped at 64 MiB. Budget additional
space for its journal and health markers.

Size and monitor the separate database, its indexes and backups, and the spool
volume. Back up request activity independently of case data. Restore a backup
and verify historical IP and fingerprint queries periodically. Back up the
fingerprint key separately to preserve matches across restarts or recovery.

To roll back, remove the web opt-in and let ingestion drain committed records.
Disabling collection also hides the admin view. Keep the database and spool
private throughout rollback.
