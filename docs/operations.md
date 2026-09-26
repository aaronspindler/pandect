# Local operations

Pandect runs on one trusted operator's machine. Run commands from the repository
root. The supported distribution is source plus Docker Compose; host Python and
uv are not required.

> [!WARNING]
> There is no authentication or authorization. Keep the published port on
> `127.0.0.1`; public-network and multi-user deployments are unsupported.
> A changed host allowlist, TLS, or a reverse proxy alone does not change that
> boundary. See [SECURITY.md](../SECURITY.md).

## First run

Start Docker, then:

```console
make setup
make up
make ps
```

Setup checks daemon availability and Compose 2.24+, validates configuration and
data paths, creates private local state, builds images, prepares verified model
permissions, and applies migrations. Run it as an unprivileged user. It preserves
existing data and configuration, and is safe to repeat at the same revision.
Stop services before setup when upgrading.

Setup, backup, restore, and supported Make runtime commands share a checkout-local
maintenance lock. This includes lifecycle commands and one-off management,
diagnostic, provisioning, and smoke commands, preventing them from starting
through Make during backup/restore. Normal exit removes the lock. Direct Docker
commands, host SQLite clients, and manual file edits bypass it: keeping those
stopped is the operator's responsibility. If a process was interrupted, inspect
the recorded PID and confirm it is no longer operating before removing an
abandoned lock; never remove an active lock to run conflicting maintenance.

A new `.env` has mode `0600`, host UID/GID, and a stable checkout-specific
project identity. A new private data root has mode `0700`; Django generates a
`0600` installation secret unless you supply an explicit override. Existing
recognized Pandect data is retained, not reinitialized.

Wait for service health and open [Pandect](http://127.0.0.1:8000/). Verify the
web/readiness/queue paths with:

```console
curl --fail http://127.0.0.1:8000/healthz/
curl --fail http://127.0.0.1:8000/readyz/
make task-smoke
```

Local operation needs no third-party credentials or internet after images are
built. Model provisioning and security scans are separate explicit network
operations. Use synthetic evidence for the [first workflow](../README.md#your-first-useful-workflow)
and test recovery before storing irreplaceable material.

## Local configuration

Setup creates `.env` from `.env.example` only if absent. Resolution follows
Compose interpolation, including quoting and explicit environment precedence;
configuration is never evaluated as shell code. Do not share full resolved
Compose output: it can contain secrets.

| Setting | Supported behavior |
| --- | --- |
| `PANDECT_DATA_DIR` | Dedicated host data root: default `./data` or a directory outside the checkout, mounted at `/data` in trusted services. |
| `COMPOSE_PROJECT_NAME` | Stable checkout-path identity exported by Make even before setup and persisted by setup; explicit overrides are honored. Namespaces containers, images, and the runner socket volume. |
| `PANDECT_PORT` | Host port, default `8000`; always published on `127.0.0.1`. |
| `PANDECT_UID`, `PANDECT_GID` | Host IDs for non-root trusted services and data access. Make uses invoking-user IDs; setup records them for direct Compose use. |
| `PANDECT_SECRET_KEY` | Optional explicit override; otherwise use the generated private `secret.key`. Back up overrides separately. |
| `PANDECT_DEBUG` | Default `false`; do not enable with sensitive evidence. |
| `PANDECT_ALLOWED_HOSTS` | Host-header allowlist, default `localhost,127.0.0.1,[::1]`; not access control. |
| `PANDECT_TIME_ZONE` | Default `UTC`. |
| `PANDECT_CLOUDFLARE_AI_ENABLED` | Optional item-consented remote processing, default `false`. |
| `PANDECT_CLOUDFLARE_ACCOUNT_ID`, `PANDECT_CLOUDFLARE_API_TOKEN`, `PANDECT_CLOUDFLARE_GATEWAY_ID` | Account, token with Workers AI Read, and configured gateway. Gateway-management permissions are needed only for provisioning the gateway. Never commit tokens. |
| `PANDECT_CLOUDFLARE_TEXT_MODEL`, `PANDECT_CLOUDFLARE_VISION_MODEL`, `PANDECT_CLOUDFLARE_TRANSCRIPTION_MODEL` | Allowlisted adapter model IDs; changes invalidate prior consent. |
| `PANDECT_CLOUDFLARE_CLIENT_TIMEOUT_SECONDS` | Total remote timeout, 1–300 seconds; default `300`. |

Compose fixes trusted upload/publication staging at `/data/work` and models at
`/data/models`. `PANDECT_WORK_DIR` and `PANDECT_MODELS_DIR` are not operator
overrides in the supported stack. Native decoder scratch is ephemeral inside
the runner. Services receive only their explicitly listed variables; the remote
token belongs to the worker and explicit remote diagnostic, not web or runner.

Setup validates resolved paths before creation or permission changes. It refuses
root, home, checkout/ancestor paths, other in-checkout locations, files, and
unsafe symlink targets. Existing roots must be operator-owned and private
(`0700`); unsafe roots are refused rather than chmodded. New roots are marked;
nonempty legacy roots must be recognizable Pandect installations. Choose a dedicated empty location rather
than bypassing safeguards. There is no recursive permission reset of arbitrary
directory contents.

After configuration changes, `make down && make up` recreates services;
`make restart` does not apply a changed container environment. Two installations
need distinct project names, ports, and data roots.

## Command reference

`make help` lists public commands and their state-changing behavior.

| Command | Purpose |
| --- | --- |
| `make setup` | Validate/create private configuration and data, build images, apply migrations. |
| `make up` / `make down` | Start services / stop and remove containers without deleting evidence. |
| `make restart` / `make ps` / `make logs` | Restart, inspect health, or follow service logs. |
| `make shell` | Interactive Django shell; entered commands may mutate data. |
| `make manage ARGS="..."` | Explicit management command, e.g. `showmigrations`; may mutate data. |
| `make migrate` | Apply committed migrations explicitly. |
| `make makemigrations` | Generate migration source through tools; review the diff. |
| `make task-smoke` | Verify real queue delivery and worker execution with a bounded wait. |
| `make processing-diagnostics` | Inspect local tools, model integrity, storage, and configuration. |
| `make processing-backfill ARGS="..."` | Create bounded, versioned runs for selected items. |
| `make models-provision ACCEPT_MODEL_LICENSES=yes` | Explicitly download pinned model/legal artifacts after reviewing terms. |
| `make model-smoke` | Real isolated inference on synthetic inputs; provisioned models required. |
| `make cloudflare-smoke` | Explicit credential/model/gateway check with fixed non-evidence input; may cost money. |
| `make test` / `make lint` / `make validate` | Focused hermetic tests, style checks, or framework/runtime validation. |
| `make format` | Rewrite Python formatting; review the diff. |
| `make check` | Complete hermetic core validation after image builds. |
| `make security-check` | Network-enabled advisory/image scans and redacted history/source secret checks, without evidence or credentials. |
| `make compose-smoke` | Disposable clean Compose install, task/isolation checks, and backup/restore. |
| `make backup BACKUP_DIR=…` | Verified full-data backup outside the checkout; requires a stopped stack. |
| `make backup BACKUP_DIR=… BACKUP_REVISION=…` | Legacy-transition backup with an explicit locally resolvable restore-source ref/SHA; tooling revision is recorded separately. |
| `make restore BACKUP=…` | Restore a verified archive into an empty configured data root, without migration. |
| `make clean` | Remove disposable source/test caches, not application evidence. |

Compose remains available for diagnosis, for example
`docker compose logs --tail=200 worker`. Prefer Make for normal operation.

## Service responsibilities

Three long-running services share the application image, but only trusted
services have application-data access:

- `web` runs Django/Gunicorn, serves local assets, accepts loopback HTTP,
  streams/validates uploads, and enqueues work. It has no remote API token.
- `worker` runs one Huey consumer, owns orchestration/provenance/publication,
  and performs explicitly consented remote calls. It does not serve HTTP.
- `media-runner` has no IP network, database, queue, canonical-media mount,
  credentials, or Docker socket. A non-decoding supervisor runs one bounded
  native-media job at a time under a disposable unprivileged identity.

The runner receives only selected input bytes through a private Unix socket.
Intake probes, decoders, native tools, inference, derived-media parsing, and
diagnostics use that boundary; there is no direct execution fallback. See
[architecture](architecture/overview.md#media-boundary) for confinement and
[processing](processing.md#storage-and-resource-limits) for resource limits.
A busy runner can make imports needing probes temporarily unavailable. Readiness
checks do not download models or call providers.

Migrations are explicit; no long-running service applies them. Durable
application stages expose processing failures independently of queue return
values. Reconciliation runs every five minutes to repair abandoned leases.
Local retries are bounded; a remote unknown outcome needs operator review
because a retry may duplicate cost. Logs are designed to use IDs and sanitized
codes, not evidence/credentials/tool output; review and redact before sharing.

## Persistent local state

The one private data root contains:

- `application.sqlite3`: catalogue, processing, provenance, and identity state;
- `huey.sqlite3`: separate queue, schedule, and task-result state;
- `media/catalog/`: canonical original files;
- `media/derived/`: append-only source-linked derivatives;
- `work/`: private upload and publication staging, cleaned through existing
  attempt journals/reconciliation rather than indiscriminate manual deletion;
- `models/`: optional weights, exact legal archives, and provisioning notices;
- `secret.key`: generated installation secret unless explicitly overridden.

Include SQLite sidecars in full-data backup; never copy an individual open
database. Runner scratch is temporary and never canonical evidence. Treat
embeddings, transcripts, metadata, consent records, and local logs as sensitive.

The default `data/` is Git/Docker-ignored; custom supported roots are outside
the checkout. Ignore rules are not encryption. Deleting the checkout or
`git clean -fdx` can destroy ignored default data. Publish Git refs/source
archives, not a raw working folder. Ordinary `make down`, `make clean`, image
rebuilds, and container recreation preserve data.

Data and backups are **plaintext**. Protect the host with appropriate disk
encryption and use encrypted backup storage. Catalogue deletion is not secure
erasure; immutable records restrict parent deletion, and stored originals are
not automatically removed. See [deletion semantics](catalog-domain.md#media-storage-and-deletion).

### Local model provisioning

Read the [artifact-specific license inventory](model-licenses.md), including
the expression model's missing model-local license and unresolved training-data
rights. Pandect does not establish commercial clearance. Review exact filenames,
revisions, licenses, and SHA-256 values before downloading:

```console
make manage ARGS="provision_processing_models --list"
```

After reviewing the linked upstream license terms, make acceptance explicit:

```console
make models-provision ACCEPT_MODEL_LICENSES=yes
make model-smoke
```

Provisioning writes downloads only beneath `work/`, fsyncs and validates them,
then installs them atomically into `models/`. Before installing weights it also
downloads the manifest-bound upstream license/NOTICE bytes, verifies their exact
size and SHA-256, and archives them under `models/legal/`. Legal archive files
are content-addressed and are never replaced: an existing mismatch is preserved
and blocks provisioning. A checksum-invalid weight is likewise left untouched
unless `ARGS="--replace-invalid"` is supplied. The generated
`PROCESSING-MODELS-NOTICE.txt` is cumulative: every partial provisioning action
atomically appends its accepted model set, exact legal source/archive/digest
references, and training-corpus limitations while preserving earlier records
and operator annotations. Keep it and `legal/` with the weights and include the
whole model directory in recovery backups. Setup and provisioning prepare
sandbox permissions offline and idempotently: only hash-verified manifest
weights and legal texts become `0644`, with public model/legal directories
`0755`; the enclosing data root stays `0700` and the operator-annotated notice
stays `0600`. Unknown private files are left private; unexpected public files
or symlinks are refused, not recursively chmodded. Review and move unrelated
content to an appropriate private directory before retrying. Never widen the
whole data directory. The underlying command is `prepare_model_permissions`,
run through the writable model-provisioner service, not the read-only web mount.

Compose mounts models read-only for the runner and trusted services; only the
explicit one-shot `model-provisioner` mounts that directory writable. Neither application startup nor a Huey task has a
download path.
`make model-smoke` is a separate optional acceptance gate, not part of
`make check`. It revalidates the pinned files and runs actual YuNet, SFace,
facial-expression, OpenVINO age-class/head-pose, Silero VAD, and CAM++ inference
on generated pixels and a generated three-second waveform. No investigation
file is opened. The isolated job has a 120-second hard limit, bounded output,
and one-thread native-runtime settings; failures expose only component
names and sanitized error codes. It performs no download or remote request.

### Cloudflare diagnostic and Meta license

Remote capability checks are deliberately separate from health/readiness and
from ordinary test execution. With Cloudflare explicitly enabled and configured,
run `make cloudflare-smoke` to query availability for every selected model and
execute a fixed three-token prompt through the configured Gateway. No catalogue
item, derivative, OCR, transcript, filename, or prompt from an investigation is
read or transferred. This check uses the configured token and may incur a small
model charge.

The configured Meta vision model requires acceptance of Meta's license and
acceptable-use policy. Pandect never performs that state-changing/legal action
by default. Review those terms first; only then run:

```console
make cloudflare-smoke ACCEPT_META_LICENSE=yes
```

The extra flag submits Cloudflare's documented agreement request before the
evidence-free Gateway check. A normal `make cloudflare-smoke` leaves acceptance
state unchanged. Neither form affects `/healthz/` or `/readyz/`.

## Backup and restore

Stop **every container belonging to the project**, including media-runner and
one-off shells/provisioners, before backup or restore. Also close host SQLite
clients and any other process using the data directory. The commands refuse a
running project; they never silently stop or restart it.

```console
make down
make backup BACKUP_DIR=/path/to/private/pandect-backups
```

Choose a dedicated backup destination outside both the checkout and source data.
An existing destination must be private (`0700`). The helper uses a read-only
source mount and creates a unique `pandect-backup-<id>/` bundle containing
`data.tar.gz`, `manifest.json`, and `SHA256SUMS`. It verifies temporary output
before atomic publication without overwriting, with `0700` directories and
`0600` files. Keep all three bundle files together.
Verification checks the SQLite files and their sidecars extracted from the
finished archive into private temporary storage, not only the original live
paths. That verification never writes to the backed-up source mount.

The complete archive includes originals, derivatives, both SQLite
stores/sidecars, models/legal records, and the generated secret. The manifest
records format/time, selected restore-source revision (normally current HEAD),
actual helper/tooling revision, current-checkout dirty flags, current runtime
image identity when locally available (otherwise `unknown`), migration state,
and nonsecret configuration. An explicit
`BACKUP_REVISION` must resolve to a local commit; it is used for the legacy
transition below, not to guess which code matches arbitrary data. The recorded
image is explicitly labeled `configured_tooling_image`, not the image that
originally wrote the evidence; `evidence_writer_image_id` is `null` because that
identity is not established. Dirty flags describe the current checkout, not the
past state of the selected commit. The bundle does not copy `.env` or tokens.
Preserve an explicit `PANDECT_SECRET_KEY` override and Cloudflare credentials
separately in protected storage. A backup from a dirty checkout also needs
those exact source changes retained separately to reproduce it.

Archives are **not encrypted**. Use an encrypted destination and test recovery
before relying on a backup. Checksums detect accidental changes; they do not
authenticate a maliciously replaced archive/checksum pair.

For restore, first preserve current data elsewhere with private permissions;
do not delete it just to make the command run. Configure a dedicated empty
destination. Pass the **bundle directory**, not `data.tar.gz`, using the path
printed by backup:

```console
make restore BACKUP=/path/to/private/pandect-backups/pandect-backup-ID
```

Restore checks checksums, manifest, safe archive members, and both SQLite
databases in private sibling staging before installing into an empty target.
It rejects traversal, unsafe links/special files, corruption, and populated
targets. It never merges, overwrites a populated installation, or applies
migrations. Follow the reported matching source revision, restore explicit
credentials separately, confirm configuration and host UID/GID, then start
that matching version:

```console
make up
curl --fail http://127.0.0.1:8000/healthz/
curl --fail http://127.0.0.1:8000/readyz/
make task-smoke
```

Keep the pre-restore data and verified backup until records, hashes,
service readiness, and task execution have been checked.

## Upgrade

Install a published tag, not whatever happens to be on `main`. Read the target
release notes, then stop services and verify a full backup before changing code:

```console
make down
make backup BACKUP_DIR=/path/to/private/pandect-backups
git fetch --tags
git switch --detach v0.1.0a1
make setup
make up
make ps
```

The initial tag above is usable only after publication; replace it with the
reviewed target release for later upgrades. Do not discard local changes to
switch tags. Setup rebuilds locked images and applies committed migrations.
Verify health/readiness and `make task-smoke`, then inspect a known synthetic
item. Use your configured port if not 8000.

### Transition from the legacy fixed Compose project

Earlier checkouts used a global `pandect` project/image. Before the first
upgrade to checkout-specific identity, stop the legacy stack explicitly using
its original checkout/configuration and record its revision. Preserve any local
source changes separately before switching revisions:

```console
PANDECT_PREUPGRADE_REVISION=$(git rev-parse HEAD)
docker compose -p pandect down --remove-orphans
```

Confirm no legacy/one-off container is still using the data. If that older
checkout lacks `make backup`, select the reviewed published target tag and run
the new backup tooling **without applying migrations**. Backup builds its small
standard-library maintenance image itself; it does not require a full
application-image build or new setup first:

```console
git fetch --tags
git switch --detach v0.1.0a1
make backup BACKUP_DIR=/path/to/private/pandect-backups BACKUP_REVISION="$PANDECT_PREUPGRADE_REVISION"
```

Only after the verified backup reports the intended old source revision should
you run `make setup`, `make up`, and the health/task checks. Keep this shell's
revision value until backup completes; if you start a new shell, recover the
previous revision from your recorded value rather than using the new HEAD.
The target tag above must exist; it is not yet published during release preparation.

Setup records a stable identity derived from the
checkout's physical path in local configuration unless a project name is
already configured or explicitly overridden. It does not stop ambiguous other
projects or delete old images/volumes for you. Never start old and new projects
against the same SQLite data. Separate installations need distinct project
names, ports, and data roots.

### Rollback

Do not downgrade a migrated database in place or run old code against a new
schema. Stop the stack, preserve its current data privately, restore the full
pre-upgrade bundle into an empty destination, and use the exact matching source
revision. Restore separate configuration/credentials as needed. Verify health,
tasks, and evidence before retiring the preserved newer data.

## Logs, health, and validation

```console
make logs
docker compose logs --tail=200 web
docker compose logs --tail=200 worker
docker compose logs --tail=200 media-runner
```

Control-C stops following logs, not the services. Logs should explain failures
without dumping settings, secrets, or evidence; still treat local copies as
sensitive and inspect/redact them before sharing.

`/healthz/` proves Django responds. `/readyz/` checks required local
dependencies including the application database and media-runner; failure
returns sanitized HTTP 503. Neither proves that a queued task completed, that
optional models exist, or that the remote provider is available.
`make task-smoke` checks the real queue/worker path.

`make check` combines Ruff, hermetic pytest, Django/migration checks, Compose
validation, runtime builds, and native-import validation. Test execution uses a
short-lived tools container with a networkless namespace and locked development
dependencies. Image construction may fetch base images, locked packages, and
OS dependencies; the offline claim applies after construction. No host Python
or uv is needed. CI targets native Linux amd64 and arm64.

`make security-check` is explicitly network-enabled for current advisory/image
scans, without application credentials/evidence. It includes redacted secret
scanning. `make compose-smoke` exercises a disposable installation and recovery.
Security scanning needs several GiB of temporary disk for exported image
archives and the expanded vulnerability database. Its private temporary cache
is disk-backed, not a large RAM tmpfs, and is removed after the scan; it receives
no application-data, credential, or Docker-socket mount.
Neither small smoke fixtures nor unit tests replace full-size capacity and
explicit real-model release gates. See [the checklist](releasing.md).

## Troubleshooting

### Docker is unavailable or permission is denied

Run `docker info` and `docker compose version`. If the daemon is unavailable,
start Docker Desktop or Docker Engine. On Linux, follow your distribution's
documented Docker access setup and start a new login session after changing
group membership. Membership in Docker's group is effectively root-level host
access; grant it deliberately. Avoid running individual Make commands with
`sudo`, which can leave `.env` or `data` owned by root.

If the checkout or restored `.env` came from another machine, update
`PANDECT_UID` and `PANDECT_GID` to the output of `id -u` and `id -g`, then run
`make down && make up` to recreate the services with the correct identity. Do
not make the data directory world-writable to bypass an ownership mismatch.

### Port 8000 is already occupied

If `lsof` is available, identify the local listener:

```console
lsof -nP -iTCP:8000 -sTCP:LISTEN
```

Otherwise, use your operating system's port-listener tool. Stop or reconfigure
the conflicting process. Alternatively, set an unused port in `.env`, for
example `PANDECT_PORT=8001`, then run
`make down && make up` and use `http://127.0.0.1:8001/`. Compose retains the
loopback binding when this port changes; do not change it to `0.0.0.0` as a
shortcut.

### SQLite reports `database is locked`

Pandect configures a five-second busy timeout for brief contention, not high
concurrency. Use `make ps` to confirm only the supported web and single worker
are running, and stop any direct Django shells, SQLite clients, backup jobs, or
duplicate stacks using the same directory. Let active work finish, then use
`make restart`. Never copy, replace, or delete an open database to clear a lock.
Persistent or frequent lock timeouts indicate a workload outside the
foundation's supported limited-concurrency model; capture the web and worker
logs before changing configuration.

### The worker is not processing tasks

Run:

```console
make ps
docker compose logs --tail=200 worker
make task-smoke
```

Confirm that `worker` is running from the same Compose project and shares the
same configured data directory as `web`. Correct the reported startup or task
error and run `make restart`. Repeating `make task-smoke` while the worker is
down only adds work to the persistent queue; it does not repair the worker.

### The Huey queue file is stale or corrupt

First rule out a stopped worker or a normal backlog. Queue recovery discards
pending, scheduled, retry, and stored result state, so use it only when logs
identify `huey.sqlite3` itself as unrecoverable and that work can be abandoned.
Stop the stack, back up the whole persistent directory, and move (do not
delete) `huey.sqlite3` plus any matching `huey.sqlite3-wal` and
`huey.sqlite3-shm` sidecars out of the data directory. Leave
`application.sqlite3` and the secret-key file untouched. Start the stack and
run `make task-smoke`; Huey will create a clean queue store. Retain the moved
files until recovery is verified.

For the default data path, after making the backup described above:

```console
make down
PANDECT_QUEUE_RECOVERY="../pandect-queue-recovery-$(date +%Y%m%d-%H%M%S)"
umask 077
mkdir -m 700 "$PANDECT_QUEUE_RECOVERY"
for file in data/huey.sqlite3 data/huey.sqlite3-wal data/huey.sqlite3-shm; do
  if [ -e "$file" ]; then mv "$file" "$PANDECT_QUEUE_RECOVERY/"; fi
done
make up
make task-smoke
```

### A migration fails

Do not start the new services or edit Django's migration table manually. Save
the complete `make migrate` output, then inspect state with:

```console
make manage ARGS="showmigrations"
```

Confirm the checkout is complete, `.env` still points at the intended data
directory, and the data directory is writable by the container user. Fix the
reported cause and rerun `make migrate`. If the release cannot be repaired,
stop the stack and restore the complete pre-upgrade backup together with its
matching source revision. Never recover by deleting only
`application.sqlite3`; doing so destroys application state and breaks the
backup boundary.

### Media runner is busy or unavailable

A long background job occupies the single media slot. Imports requiring a probe
return an actionable busy/unavailable response rather than waiting indefinitely
or decoding directly in web. Retry after the indicated interval; do not
repeatedly submit the same batch.

Check `make ps` and recent `media-runner` logs. Restore the supported Compose
capabilities/mounts/resource configuration if confinement startup fails. Do not
add networking, privileged mode, host PID/IPC, a Docker socket, or an execution
fallback. Missing models need explicit provisioning, not internet access for
the runner. A crash or resource limit should fail the isolated job; keep its
sanitized code and rerun only after diagnosing the cause.

### Upload, scratch, or output limits are reached

The defaults are 2 GiB/file, 8 GiB/batch, 50 files, and 2 GiB free disk reserve.
The runner separately has 4 GiB scratch, 8 GiB memory, and 1 GiB returned-output
limits. Encoded size is not decoded size: a valid small file can exceed these
bounds. A rejected batch must not create partial catalogue records. Do not
delete originals or databases to make room. Move unrelated host data, expand
storage, or preserve the original outside Pandect until the workflow can handle
it. Report reproducible limit failures with generated media, not real evidence.

### Setup rejects a data or model path

Use default `./data` or a dedicated external directory without symlink
components. Existing data must be owned by the invoking operator and private
(`0700`); setup refuses unsafe roots rather than changing their permissions.
Review the exact directory and its contents yourself before deliberately
correcting ownership/permissions. Never recursively chmod a home, checkout, or
unknown directory. Model permission preparation similarly refuses unexpected
public files: retain/move unrelated content privately and retry setup, rather
than widening it for parser jobs.

## Architecture boundary

The [living architecture](architecture/overview.md) links both accepted ADRs.
Pandect is a single-machine, limited-concurrency Django application with local
SQLite state, versioned extraction, and a closed operator-curated identity
gallery. It is not an internet-facing or multi-operator deployment design.
Source connectors, search, open-world identity lookup, public APIs, distributed
workers, and generic plugin infrastructure remain outside the current boundary.
Optional Cloudflare processing is narrowly consented, not a hosted default.
