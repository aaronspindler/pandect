# Request activity

Request activity is an optional origin log for site operators. It records HTTP
requests accepted by Gunicorn for the configured public host, including static
files and signed-in routes. Cloudflare-only responses and requests rejected before
Gunicorn's access hook fall outside that coverage. Live event requests appear when
their streams close. The status is what Gunicorn reports, which can be `500` on an
early disconnect.

Each record contains a time, method, server-reported status, route label, client
IP when verified, and a versioned fingerprint. The fingerprint uses a dedicated
key and bounded User-Agent and Accept-Language values. It can match across IP
changes. Similar browsers can share a fingerprint, and a browser update or key
change can produce a different one. Request and response content, raw targets,
header strings, cookies, account IDs, and case IDs are excluded from these records.

## Review requests

In **Pandect administration**, open **Request activity**. Search by exact IP or
versioned fingerprint, optionally with a date range, to find matching requests
throughout the retained history. The log is read-only and has no scheduled expiry.
Grant active staff access by adding them to the Django group **Request activity
operators**. Group membership is checked on every request. Keep the group limited
to site operators responsible for this data.

The returning-visit summary counts fingerprints seen on eligible public HTML
requests on at least two UTC dates in a 30-day reporting window. It reports
different fingerprint versions separately and shows missing signals. It estimates
repeat browser configurations and can include automated traffic. No browser
cookie is required.

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

The monitor command `python manage.py request_activity check` checks the private
buffer and ingester without using request fields. Run it every 30 seconds and
connect its nonzero exit status to the operator's monitoring system. It reports
missing or stale heartbeats after 45 seconds, buffered records waiting over 60
seconds, and known capture gaps in the last five minutes. Keep capture warnings
and monitoring output limited to operational counts and timestamps.

## Recover and roll back

Gunicorn queues an event in memory without waiting for disk or PostgreSQL. A
background writer commits it to the private spool. The ingester acknowledges
spooled events only after its PostgreSQL transaction commits. Replaying a batch
after a restart does not duplicate rows. If either buffer fills, the website
keeps serving and records may be dropped. A worker crash can lose events that
have not committed to the spool. Review health and known capture gaps in the admin
before interpreting absence of requests.

The web worker limits its in-memory queue to 512 records. The SQLite spool
caps its main file at 64 MiB. Leave additional space for SQLite's transaction
journal and the health marker on the private volume.

Size and monitor the separate database, its indexes and backups, and the spool
volume. Back up request activity independently of case data. Restore a backup
and verify historical IP and fingerprint queries periodically. Back up the
fingerprint key separately to preserve matches across restarts or recovery.

To stop collecting, remove the web opt-in and let the ingester drain committed
spool records. Keep the private database and spool volume under operator control
through rollback. The existing web health check continues to describe Pandect
readiness. Check request activity separately.
