# Pandect

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="ui/static/ui/brand/logo-ivory.png">
  <img src="ui/static/ui/brand/logo-oxblood.png" alt="Pandect logo" width="432">
</picture>

**A self-hostable investigation workspace.**

Pandect brings the material you have gathered into private cases. Keep original
files, messages, and location observations together, search within a case, and
review findings alongside the sources behind them. Invite collaborators to
inspect the evidence or contribute to the investigation.

Pandect is for organizing and examining collected material, including open-source
intelligence (OSINT). It is built with Django and PostgreSQL and runs on your own
infrastructure with Docker Compose.

**In active development:** The workflows below are implemented. Some analysis
needs separately configured services; inferred event timelines, relationship
graphs, and broader evidence discovery remain planned. A managed hosting service
is also planned.

## What you can use today

- **Private cases and collaboration.** Create cases, invite collaborators by
  email, and grant viewer or editor access. Each case has one owner who manages
  access. Evidence and file downloads require current case permission.
- **Evidence collection and preservation.** Upload documents, images, and audio;
  capture text, messages, calls, and locations; and retain call transcripts and
  recordings. Keep originals with checksums and source details, then archive or
  restore evidence without replacing the original. Read supported UTF-8 text and
  PDF page previews, and play supported WAV recordings through private readers.
- **People, vehicles, and maps.** Maintain case people, contact details, social
  account records, and vehicle identifiers. Map recorded coordinates and
  person-linked points of interest. Open evidence and findings explicitly linked
  to a person. Reported details remain distinct from verified identity or presence.
- **Search and source-linked findings.** Search retained text and supported
  extraction within a case. Eligible new evidence receives local checks for
  native PDF/UTF-8 text, printed English in JPEG/PNG images, possible vehicle
  plates, supported media metadata, and identifiers, dates, and keywords in
  available text. Browse all retained findings with search, filters, review states,
  and earlier run versions. Findings link back to their exact sources.
- **Connections and chronology.** Inspect repeated identifier candidates in
  analyzed material and follow recorded communication/location times in one
  chronology. Matching strings do not establish identity; uncertain extracted
  dates stay separate from recorded events.
- **Shared selections and briefs.** Save exact evidence and finding references
  with collaborators. Export a printable HTML brief with citations, review
  decisions, and a downloadable provenance manifest.
- **Imports and pattern review.** Review Asset Location CSV exports before
  importing vehicle observations, or import the supported iMessage HTML profile.
  Inspect possible vehicle trips, call activity, observation coverage, and exact
  file duplicates. Deliberately selected comparisons cover wording, writing
  patterns, conversation timing, and reported locations.
  Processing separates queued work from completed results and offers recovery
  actions. Completed imports offer deliberate local checks on imported records.
- **Self-hosting.** Run the application, PostgreSQL, and worker with Docker
  Compose. Core case work and the local checks above need no external AI service
  or hosted Pandect subscription.

Formats, resource bounds, and coverage vary by method. Native PDF extraction
does not read scanned pages; local image text recognition supports printed
English, not handwriting. Possible trips and plate readings are leads to inspect,
not verified routes, drivers, or identities. See the [supported analysis
guide](docs/analysis.md) for inputs and limits.

### Available with additional configuration

English speech transcription, topic and claim suggestions, conversation summaries,
semantic conversation comparisons, and question-driven synthesis need an
operator-configured private model runtime. Synthesis uses explicitly selected
text and findings, with source citations, contrary evidence, and unresolved
questions; it does not automatically search the whole case. External AI is a
separate, explicitly selected and approved route through
Cloudflare AI Gateway; a local failure never triggers cloud fallback.

Address lookup and geographic context need configured geographic services. Map
backgrounds also use an operator-configured tile provider. See [local analysis
setup](docs/local-analysis.md) and [map configuration](docs/maps.md).

### Planned and exploratory

Planned work includes inferred event timelines, relationship graphs, and broader
evidence discovery for synthesis beyond explicitly selected material. Broader
goals include search across accessible cases. Semantic passage retrieval, near-duplicate
image comparison, structured table extraction, and other specialist tools remain
exploratory. These are not available features or a promised release schedule;
see the [analysis roadmap](docs/evidence-analysis-roadmap.md).

## Getting started

Install Docker with Compose v2, `make`, and
[uv](https://docs.astral.sh/uv/getting-started/installation/) 0.13.7. uv manages
the project's pinned Python version and dependencies.

Clone the repository and prepare a local environment:

```sh
git clone https://github.com/aaronspindler/pandect.git
cd pandect
uv sync --locked
cp .env.example .env
```

Generate a unique Django secret key and database password in the new `.env`
file, then start the application:

```sh
uv run --locked python - <<'PY'
import secrets
from pathlib import Path

path = Path(".env")
content = path.read_text()
for name in ("DJANGO_SECRET_KEY", "POSTGRES_PASSWORD"):
    content = content.replace(f"{name}=\n", f"{name}={secrets.token_urlsafe(50)}\n")
path.write_text(content)
path.chmod(0o600)
PY
make run
```

Open <http://localhost:8000/>, create an account, and verify your email. In the
default local configuration, verification and invitation messages appear in:

```sh
docker compose logs web
```

Keep these logs private because they contain account and invitation links. After
verification:

1. Choose **All cases → Create case**.
2. The case opens to **Case Tools**, with recent evidence, findings, and links to
   maps, chronology, connections, people, vehicles, selections, and reports.
3. Choose **Evidence → Add evidence** to upload files or capture text and locations.
4. Open **Processing** to follow active checks/imports and their history. Use
   **Case Tools → Case details → Settings** to invite collaborators.

Ordinary investigators do not need staff or administrator permissions.

Use `docker compose stop` to stop the application while retaining its database
and evidence. Run `make run` to start it again. For configuration, alternate
ports, or running Python on the host, see the [local development
guide](docs/development.md). The supplied Compose setup is for local
development; see [operations](docs/operations.md) before configuring a hosted
deployment.

## Documentation

- [Documentation index](docs/README.md)
- [Local setup and configuration](docs/development.md)
- [Investigation workflows and permissions](docs/cases.md)
- [Evidence and analysis](docs/analysis.md)
- [Operations, backup, and recovery](docs/operations.md)
- [Validation and CI](docs/validation.md)
- [Project descriptions and reusable marketing copy](docs/marketing-copy.md)

## Contributing

Bug reports, feature requests, documentation improvements, and pull requests are
welcome. Start with the [contributor guide](AGENTS.md), inspect the relevant
documentation, and read [DESIGN.md](DESIGN.md) before changing the interface.

After completing local setup, run the core checks against the local PostgreSQL
database:

```sh
uv run --locked --env-file .env make check
```

See [validation](docs/validation.md) for the full workflow and additional
checks. Use synthetic or sanitized examples in contributions; keep investigation
material, credentials, uploads, and private logs out of issues and pull
requests.

## Contact

- **Maintainer:** [Aaron Spindler](https://github.com/aaronspindler).
- **Questions, bugs, and feature requests:**
  [Open an issue](https://github.com/aaronspindler/pandect/issues).
- **Security vulnerabilities:**
  [Follow the security reporting policy](SECURITY.md).

## License

Copyright (C) 2026 Aaron Spindler.

Pandect is licensed under the [GNU Affero General Public License, version 3
only (AGPL-3.0-only)](LICENSE).

See the license for source-sharing requirements, including modified versions
offered over a network, and warranty terms. It covers the software, not rights
to investigation data or uploaded evidence.

Bundled third-party assets retain their own licenses, including [Tabler
icons](ui/static/ui/icons/LICENSE) and [Leaflet](geo/static/geo/vendor/leaflet/LICENSE).
