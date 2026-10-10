# Pandect

Pandect is a local-first investigation application for **one trusted operator
on one machine**. Catalogue images, video, audio, messages, notes, and events;
retain originals; and review versioned extraction, provenance, and explicitly
uncertain model observations separately from your own conclusions.

> [!WARNING]
> This is alpha software with **no authentication or authorization**. Keep it
> on `127.0.0.1`; LAN, public-internet, shared-machine, and multi-user deployment
> are not supported. Use synthetic or replaceable material until you have
> tested backup and restore. Read the [security policy](SECURITY.md).

The application uses Django, server-rendered HTML, local HTMX assets, SQLite,
and one Huey worker. A separate, network-disabled media runner handles native
decoding and inference. Cloudflare processing is optional, disabled by default,
and requires item-specific consent. No models download at startup.

## Install and start

This project is distributed as source and Docker Compose configuration, not a
PyPI package or prebuilt container image. **Do not use `pip install pandect`**;
that package name is not this installation workflow.

You need:

- macOS or Linux with Git, Make, a POSIX shell, and a running Docker daemon;
- Docker Compose plugin **2.24 or newer** (`docker compose`, not `docker-compose`);
- a **16 GiB RAM host**, with **12 GiB allocated to Docker Desktop**, as the
  large-media capacity-test target; Linux amd64 and arm64 are the CI targets;
- space for Docker images/build cache, originals, derivatives, upload staging,
  optional models, and separate backups. Intake retains at least 2 GiB free;
- internet access for the initial image build and explicitly requested model
  provisioning or security scans. Built local processing can run offline.

The first build downloads the locked Python/native dependencies and OS media
tools, takes several minutes, and uses several GiB of Docker storage. Model
weights are a separate optional download of approximately 77 MiB. Compressed
media may expand far beyond its upload size; resource limits can reject a file
that fits the upload limit.

The initial release being prepared is **`0.1.0a1`**, tag **`v0.1.0a1`**. The
commands below are for that tag **after it is published**. For an unreleased
checkout, stay on your existing reviewed revision and start at `make setup`.

```console
git clone --branch v0.1.0a1 https://github.com/aaronspindler/pandect.git
cd pandect
make setup
make up
make ps
```

Wait for service health, then open [Pandect](http://127.0.0.1:8000/).
`make setup` creates private local configuration and data, builds images, and
applies migrations. It preserves existing configuration and evidence. Do not
run it as root. Existing installations should follow the
[upgrade procedure](docs/operations.md#upgrade), including the legacy Compose
project transition.

```console
curl --fail http://127.0.0.1:8000/healthz/
curl --fail http://127.0.0.1:8000/readyz/
make task-smoke
```

These check web liveness, application readiness, and actual queue execution.
They do not prove the accuracy of extracted or model-generated observations.

## Your first useful workflow

1. Open the catalogue and add a small **synthetic** image, audio file, video,
   or text message to try processing. Notes/events are catalogue-only records;
   a blank media-message caption has no separate text-processing run. Bulk
   upload supports media files only.
2. Review the title and known metadata. Leave unknown times and facts blank;
   an original occurrence time is different from the time you catalogued it.
3. Open the item's detail page. Watch local stages finish and inspect their
   source hashes, extraction results, warnings, and provenance. A rerun appends
   history instead of replacing prior evidence.
4. Optional face/voice model stages need explicitly provisioned models; without
   them those stages can fail visibly while catalogue storage and deterministic
   extraction remain usable. Review [model licensing](docs/model-licenses.md)
   before choosing to provision. Missing models do not trigger a remote fallback.
5. Review any proposed timestamp or identity match as a hypothesis, not a fact.
   Operator confirmation is recorded separately. Remote processing, if configured,
   requires reviewing the exact item-specific transfer manifest first.
6. Stop the stack and make a backup before adding irreplaceable material.

Uploads are limited to **2 GiB per file, 8 GiB per batch, and 50 files per
batch**. Media probing and background processing share one execution slot;
imports needing probing may report busy until the active job finishes.

Stop services without deleting data:

```console
make down
```

## Using the workspace

1. Choose **Add material** from Home or the navigation bar. Add a file, event,
   note, or message, or select up to 50 media files at once. For an image or video
   message, add its original file first, then link it to the message.
2. Review the saved item's details. Required fields have an asterisk; other
   fields can remain blank when unknown. Dates use the timezone stated beside
   the field. **Finish without changes** keeps the item and its saved details,
   but does not save edits made on the review page.
3. Open an item in **Catalogue** to review its original record, processing
   progress, extracted information, and AI suggestions. Evidence tiers remain
   separate, and previous runs remain available in **Processing history**.
   Selecting a history run requires **Show processing run**; select **Current
   results** to return to the combined current view.

New files and text messages start local processing when added. **Process
existing material** checks batches for missing or outdated processing. Notes
and events are saved for manual review. Optional remote AI processing requires
separate consent to the displayed transfer details; local processing preserves
the originals. **Application status** on Home checks the web application, not
the background worker.

## Privacy, limits, and help

Originals, derived media, databases, identity features, and secrets are stored
locally **without application-level encryption**. Protect the host and backups.
Local-first does not mean the trusted web/worker processes have no network
access, and the media sandbox does not protect against a compromised host or
Docker daemon. Remote providers receive only the consent-described inputs, but
their retention, pricing, and model terms remain external dependencies.

Pandect is not a forensic certification, a guarantee of admissibility, a truth
detector, or open-world identification. Scores are not identity probabilities.
It currently excludes source connectors, scraping, search, evidence graphs,
authentication, tenancy, and public APIs. See the
[current architecture](docs/architecture/overview.md) before proposing changes
to those boundaries.

- [Operations](docs/operations.md): configuration, commands, backup/restore,
  upgrades, and troubleshooting.
- [Processing](docs/processing.md) and [catalogue domain](docs/catalog-domain.md):
  evidence lifecycle, limits, consent, and extension points.
- [Contributing](docs/contributing.md): development, tests, and contribution terms.
- [Security](SECURITY.md): threat model, supported releases, and private reporting.
- [Changelog](CHANGELOG.md) and [release checklist](docs/releasing.md): release
  status and required validation. `make help` lists operational commands.

## License

Copyright 2026 Aaron Spindler. Original Pandect code and documentation are
licensed under [Apache-2.0](LICENSE). [Third-party dependencies](docs/third-party-notices.md)
and [optional models](docs/model-licenses.md) retain their upstream terms;
Pandect's license does not establish rights to your evidence or model training data.
