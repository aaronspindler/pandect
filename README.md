# Pandect

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="static/pandect/brand/logo-ivory.png">
  <img src="static/pandect/brand/logo-oxblood.png" alt="Pandect logo" width="432">
</picture>

Pandect is a self-hostable investigation platform designed to make
open-source intelligence (OSINT) approachable. Its goal is to bring
evidence, people, connections, and analysis into one clear investigation
workflow, with the care and usability expected of an everyday application.

Investigators should be able to understand where information came from, preserve
uncertainty, and collaborate without losing control of their case material.
Pandect is built with Django and PostgreSQL, with one codebase for self-hosting
and operator-hosted deployments.

**Status:** Pandect is in active development. The current application provides
the core case, collaboration, people, vehicle, and evidence workflows described below.
Analysis and visualization capabilities are being built incrementally.

## Features

- **Case management:** Create investigations with their own people, evidence, and
  access permissions. Search your cases and filter owned or shared work.
- **Collaboration:** Invite people by email, grant viewer or editor access, and
  manage collaborators from case settings. Each case has one owner.
- **People records:** Capture names, contact details, and investigator notes
  within a case, including partial or uncertain identities.
- **Vehicle records:** Capture make, model, year, color, plate, VIN, and investigator
  notes within a case, including partial or uncertain identifiers.
- **Evidence collection:** Upload images, audio, and documents, or capture original
  text. Queue multiple files and record source details and collection times.
- **Communication evidence:** Capture individual text messages, phone calls, and
  FaceTime calls between case people, with optional event times/call durations.
  Add retained transcripts and recordings when available, including later additions.
- **Location observations:** Record addresses or coordinates, optionally linked to
  a person or vehicle in the same case, with source details and an observation time. These
  capture reported information rather than verified whereabouts.
- **Vehicle location imports:** Review Asset Location CSV exports, explicitly select
  their timezone and case vehicles, and retain source rows and telemetry alongside
  the original CSV. A background worker prepares and publishes imports up to
  100,000 data rows and 100 MiB, subject to the deployment's upload limit.
- **Original preservation:** Retain originals with SHA-256 checksums. Edit
  descriptive details separately, archive or restore evidence, and download files
  through case-authorized routes. Case owners can confirm whole-case deletion.
  Superusers with current case access can review and confirm individual evidence
  deletion, including its dependent records and files.
- **Investigator workspace:** Search and filter evidence in a responsive interface
  with a three-pane desktop layout, mobile views, and keyboard navigation.
- **Case analysis:** Inspect call, vehicle, and duplicate reports and search
  retained and extracted text. Owners and editors can choose **Analyze evidence**
  beside an original to start applicable local checks. Review source-linked
  suggestions with retained processing history. See [supported inputs and
  configuration](docs/analysis.md).
- **Self-hosting:** Run locally with Docker Compose. Ordinary case management
  requires no AI credentials or hosted Pandect subscription.

### Planned capabilities

The longer-term direction includes broader extraction and specialist analysis,
search across accessible cases, entity and relationship exploration, graphs,
unified timelines, annotations, and case-report synthesis. The
[analysis catalogue](docs/evidence-analysis.md) describes possibilities beyond the
current supported workflows.

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
2. Use **Add evidence** to upload files or capture text and location observations.
3. Open **People → Add person** to record people involved in the investigation.
4. Open **Vehicles → Add vehicle** to record vehicles encountered in the investigation.
5. Use **Settings → Invite collaborator** to share the case.

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
- [Cases, invitations, and permissions](docs/cases.md)
- [People records](docs/people.md)
- [Vehicle records](docs/vehicles.md)
- [Evidence handling and retention](docs/evidence.md)
- [Vehicle locations and CSV imports](docs/vehicle-location-imports.md)
- [Architecture and implemented scope](docs/architecture.md)
- [Operations, backup, and recovery](docs/operations.md)
- [Validation and CI](docs/validation.md)

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

A project-wide license has yet to be selected for the rebuilt application.
Bundled third-party assets retain their licenses, including the [Tabler
icons](static/pandect/icons/LICENSE) and [Leaflet](static/vendor/leaflet/LICENSE).
