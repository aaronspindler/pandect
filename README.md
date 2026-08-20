# Pandect

> [!WARNING]
> **Pandect currently has no authentication or authorization.** The supported
> stack must remain bound to a trusted local interface. Do not expose it to a
> LAN, the public internet, or any other untrusted network unless you have added
> and verified an external access-control layer.

Pandect is a local-first, self-hosted investigation application for one trusted
operator. It combines a dependable server-rendered Django and SQLite foundation
with a source-neutral catalogue for images, video, audio, events, notes,
messages, and communication participants. Intake returns after bounded local
classification, then a durable one-worker pipeline extracts technical metadata,
OCR, text and writing profiles, timestamp claims, and acoustic evidence without
changing originals. Optional model-generated processing remains separately
labeled and consent-bound. Data-source integrations remain deferred.

## Quick start

### Prerequisites

- macOS or Linux with a POSIX shell and standard Unix utilities
- Git
- Make
- Docker with Compose plugin version 2.24 or newer (`docker compose version`
  must work; the legacy `docker-compose` command is not supported)
- A running Docker daemon, such as Docker Desktop or Docker Engine
- `curl` for the optional command-line health checks below

Clone the repository, initialize it, and start the stack:

```console
git clone https://github.com/aaronspindler/pandect.git
cd pandect
make setup && make up
```

`make setup` creates local configuration and persistent-data paths, builds the
shared application image, and applies Django migrations. Open
<http://127.0.0.1:8000/> after `make up` starts the web and worker services.
Use `make ps` and wait until both services are healthy before checking them.

Verify the web, database, and worker paths:

```console
curl --fail http://127.0.0.1:8000/healthz/
curl --fail http://127.0.0.1:8000/readyz/
make task-smoke
```

`/healthz/` proves the web process responds, `/readyz/` verifies access to the
application database, and `make task-smoke` sends a harmless demonstrative
task through the real Huey queue and worker.

Stop the stack without deleting local data:

```console
make down
```

## Where to go next

- [`docs/operations.md`](docs/operations.md) covers commands, processes,
  persistence, backup and restore, upgrades, health checks, logs, and
  troubleshooting.
- [`docs/contributing.md`](docs/contributing.md) covers validation and the
  boundaries for future Django apps and source integrations.
- [`docs/architecture/0001-foundation.md`](docs/architecture/0001-foundation.md)
  records the canonical foundation architecture and explicit non-goals.
- [`docs/catalog-domain.md`](docs/catalog-domain.md) documents the catalogue and
  communication app boundaries, models, timestamps, deletion behaviour, and
  extension points.
- [`docs/processing.md`](docs/processing.md) documents durable stages, evidence
  tiers, resource bounds, local models, identity matching, and optional remote
  consent.
- [`docs/architecture/0002-async-multimodal-processing.md`](docs/architecture/0002-async-multimodal-processing.md)
  records the processing/provenance architecture decision.
- `make help` lists every public Make target and identifies state-changing
  operations.

The current application deliberately excludes evidence graphs, search, source
adapters, scraping, polling, open-world identity lookup, authentication,
tenancy, a public API, cloud infrastructure as a default, and a generalized
plugin system. See the architecture decisions before proposing a change to
those boundaries.
