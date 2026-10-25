# Pandect

Pandect is an open-source investigation and intelligence platform designed to
make open-source intelligence (OSINT) approachable, with clear workflows and
traceable evidence.

**Status:** Early rebuild. The Django foundation is runnable with uv and
PostgreSQL, locally or through Docker Compose. It includes a custom user model,
django-allauth authentication, Django admin, and a database readiness endpoint.

Planned capabilities include case management and collaboration, evidence ingestion,
search, relationship analysis, timelines, maps, annotations, and reporting. The
target architecture uses Django and PostgreSQL, with Docker Compose for local
operation and one codebase for self-hosted and operator-hosted deployments.

- [Project documentation](docs/README.md)
- [Local setup and dependency management](docs/development.md)
- [Contributor and agent guide](AGENTS.md)
