# Pandect

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="ui/static/ui/brand/logo-ivory.png">
  <img src="ui/static/ui/brand/logo-oxblood.png" alt="Pandect logo" width="432">
</picture>

Pandect is an open-source, self-hostable investigation workspace. Organize
evidence in private cases, collaborate, and trace findings to their sources.

## What you can do

- Retain original documents, images, recordings, messages, and location records.
- Invite collaborators with viewer or editor access to each case.
- Organize people and vehicles, and explore recorded events in a chronology.

Core case work needs no AI credentials or subscription. Case owners can enable
external AI for [passage search within and across eligible cases](docs/cloudflare-search.md),
[model analysis](docs/analysis.md#use-external-ai), and
[cited Evidence chat](docs/analysis.md#evidence-chat).

Pandect is in active development. See the [roadmap](docs/evidence-analysis-roadmap.md)
for planned features.

## Get started

1. Start Pandect with Docker Compose using the [local setup guide](docs/development.md).
2. Open [localhost:8000](http://localhost:8000/), create an account, and verify your
   email using the instructions in the guide.
3. Choose **All cases → Create case**, then **Evidence → Add evidence**.

For hosted deployments, follow the [operations guide](docs/operations.md).

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

Bundled [Tabler icons](ui/static/ui/icons/LICENSE),
[Leaflet](geo/static/geo/vendor/leaflet/LICENSE), and
[htmx](ui/static/ui/vendor/htmx/LICENSE) retain their own licenses.
