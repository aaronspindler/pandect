# Current architecture

This is the living overview for the public alpha. Read
[ADR 0001](0001-foundation.md) for the trusted-machine foundation and
[ADR 0002](0002-async-multimodal-processing.md) for processing/provenance
decisions. Those ADRs record the milestones at which they were accepted;
foundation-only deferrals are not a description of today's entire application.

## Responsibilities and data flow

```text
Trusted local browser -- loopback HTTP --> web (Django/Gunicorn)
                                           | catalogue, consent, stage UUIDs
                                           v
                       application.sqlite3 + huey.sqlite3
                                           |
                                           v
                                      worker (Huey)
                                           | explicit consent only
                                           +------------------> Cloudflare

web intake probe / worker compute -- private Unix socket --> media-runner
                                  selected bytes only       no IP network
                                  validated results         no evidence/DB mounts
```

- `core` owns health and platform plumbing; `catalog` owns catalogue roots and
  atomic intake; concrete media, event, note, and communication apps own their
  type-specific records. See [catalogue domain](../catalog-domain.md).
- `processing` owns durable runs/stages, consent, extraction orchestration,
  reconciliation, and provenance. `identities` owns the closed, operator-curated
  reference gallery and immutable observation/decision history.
- One Huey worker schedules work; SQLite application rows, not queue return
  values, are authoritative workflow state. Migrations remain explicit.
- Originals stay canonical. Derived media and observations live separately;
  trusted publication verifies hashes and records versioned provenance.
  Parent deletion cannot cascade through immutable evidence records.

There is no application authentication or public API. Local browser assets are
served from the repository with no frontend build service or CDN. PostgreSQL,
Redis, hosted storage, telemetry, distributed execution, and a generic plugin
system are not defaults or requirements.

## Media boundary

The separate media runner handles native tools, pixel/audio decoding, and model
inference, including intake probes and diagnostics. The web/worker remain
trusted orchestration processes and must not decode untrusted media directly.
The runner receives only selected input bytes and fixed operation parameters;
it cannot choose final evidence paths, write database rows, or authorize remote
transfers. Responses and file trees are untrusted and checked before publication.

Compose gives the runner a private socket directory, read-only public models,
a read-only root, bounded ephemeral scratch, no network, and no database,
canonical-media, credential, queue, or Docker-socket mount. A non-decoding PID 1
supervisor launches one job at a time with a fresh unprivileged identity,
restricted environment/descriptors, bounded resources, and child IPC restrictions.
It terminates/reaps descendants and validates outputs before another job can
start. Failure to establish or clean up confinement fails closed, not into a
direct execution fallback. Docker's default seccomp and no-new-privileges remain
enabled; the supervisor retains only the capabilities needed for identity,
ownership, and child termination.

The hard defaults are 8 GiB runner memory with no additional swap, 4 GiB scratch
(8,192 inodes), two CPUs, 128 processes/threads, 256 open descriptors, and no
core dumps. Returned artifacts are limited to 1,024 regular files and 1 GiB in
total per job, also capped at 1 GiB per file. Existing operation deadlines are
retained, never exceeding two hours. Web and worker memory defaults are 512 MiB
and 1 GiB. These are containment limits, not a promise that every accepted file
will decode successfully. Single-slot saturation produces an explicit busy
response for imports needing a probe.

The boundary reduces the exposure of evidence and credentials to hostile
parsers; it does not make a compromised kernel, Docker daemon, or trusted host
safe. Read [SECURITY.md](../../SECURITY.md) and keep the host patched.

## Network and persistence

Trusted services have only their explicitly listed configuration. The remote
token belongs to the worker and explicit remote diagnostic, not the web or
runner. Models and upstream license texts download only through explicit
provisioning; remote evidence transfer requires a current per-item manifest.
The trusted services are not claimed to be egress-isolated.

The private data directory contains both SQLite stores, originals, derived
artifacts, model/legal archives, the generated secret, and trusted publication
staging. It remains plaintext. Runner scratch is ephemeral and never canonical.
Stopped-stack backup captures the complete directory; restore never merges into
populated data. See [operations](../operations.md) for configuration and recovery.

Append-only ORM controls and hashes are application integrity measures, not
cryptographic external attestation or protection from the machine's operator.
Model output is a hypothesis; reference-gallery matches do not identify unknown
people beyond that operator-owned gallery.
