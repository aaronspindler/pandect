# Pandect

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="ui/static/ui/brand/logo-ivory.png">
  <img src="ui/static/ui/brand/logo-oxblood.png" alt="Pandect logo" width="432">
</picture>

Pandect is an open-source, self-hostable investigation workspace. Bring files,
messages, and location records into private cases to search evidence, collaborate,
and review findings with links back to their sources.

## What you can do

- Collect documents, images, audio, and messages while preserving originals and
  source details.
- Invite collaborators with viewer or editor access to each case.
- Organize people and vehicles, and explore recorded events in a chronology.
- Save evidence and findings in shared selections and export printable briefs
  with citations.

Core case work needs no AI credentials or hosted subscription. Optional
[AI analysis](docs/local-analysis.md) and [map services](docs/maps.md) need
additional configuration. External AI requires explicit selection and approval;
local failures never trigger cloud fallback.

Pandect is in active development. See the [roadmap](docs/evidence-analysis-roadmap.md)
for planned features.

## Get started

1. Follow the [local setup guide](docs/development.md) to install the required
   tools, configure Pandect, and start it with Docker Compose.
2. Open [localhost:8000](http://localhost:8000/), create an account, and verify your
   email using the instructions in the guide.
3. Choose **All cases → Create case**, then **Evidence → Add evidence**.

The supplied Compose setup is for local development. For hosted deployments,
follow the [operations guide](docs/operations.md).

## Learn more

- [Documentation](docs/README.md)
- [Cases and collaboration](docs/cases.md)
- [Evidence collection](docs/evidence.md)
- [Supported analysis and limits](docs/analysis.md)

## Contribute and get help

[Report a bug or request a feature](https://github.com/aaronspindler/pandect/issues).
For contributions, read the [contributor guide](AGENTS.md) and
[validation guide](docs/validation.md), including the repository safeguards.
Keep investigation material, credentials, and private logs out of contributions.

Report vulnerabilities through the [security policy](SECURITY.md).

## License

Copyright (C) 2026 Aaron Spindler. Licensed under [AGPL-3.0-only](LICENSE).
The license covers the software; it does not grant rights to investigation data
or uploaded evidence.

Bundled [Tabler icons](ui/static/ui/icons/LICENSE) and
[Leaflet](geo/static/geo/vendor/leaflet/LICENSE) retain their own licenses.
