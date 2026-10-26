# Pandect

Pandect is an open-source investigation and intelligence platform designed to
make open-source intelligence (OSINT) approachable, with clear workflows and
traceable evidence.

**Status:** Core investigator interfaces are implemented on the Django and
PostgreSQL foundation. Verified accounts can create cases, invite collaborators,
manage people, and upload or capture image, audio, document, and text evidence.
The responsive interface uses the refined three-pane design, with private
original downloads and explicit source/investigator details.

Owners and editors can edit descriptive evidence details and archive or restore
material. Individual evidence is retained; only owner-confirmed deletion of a
whole case removes its originals. Case permissions apply to every product request.

To start, follow [local setup](docs/development.md), create and verify an account,
then choose **Create case**. Add evidence, record people, and invite collaborators
from case settings. Ordinary investigators do not need Django admin access.

Extraction, AI, graphs, maps, timelines, and reporting remain planned. One codebase
supports local self-hosting and operator-hosted deployment without requiring an
AI service for routine investigation work.

- [Project documentation](docs/README.md)
- [Local setup and dependency management](docs/development.md)
- [Contributor and agent guide](AGENTS.md)
