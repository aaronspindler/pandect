# Pandect contributor and agent guide

This guide applies throughout the repository. Inspect the affected code and its
[owning documentation](docs/README.md) before changing it. Keep durable requirements
here, design standards in [DESIGN.md](DESIGN.md), and procedures and implementation
details in `docs/`. Update documentation alongside behavior; distinguish current
capabilities from proposals.

## Product direction

Pandect is a self-hostable investigation and intelligence platform intended for
open-source release. Make careful investigative work approachable through clear
language, thoughtful defaults, progressive disclosure, and polished accessibility.
Design around the investigator's task and next useful action.

Read [DESIGN.md](DESIGN.md) before visible changes. Open cases to Case Tools, the
main workspace for exploring evidence and results. Keep Evidence and Processing
directly available; separate active work and processing history from case insights.
Preserve source attribution, saved legacy notes, and finding review reasons;
do not restore freeform note authoring. Semantic retrieval, inferred event timelines, graphs, and specialist
analysis are incremental product goals, not claims of implemented capability.

## Simplicity and maintainability

- Choose the simplest correct solution for current requirements. Prefer idiomatic
  Django, the standard library, and established project conventions.
- Keep responsibilities cohesive, names readable, and control flow explicit. Add
  abstractions only for concrete reuse or domain complexity; add dependencies,
  services, infrastructure, or caching only for a demonstrated need. Document
  significant tradeoffs.
- Simplicity must preserve correctness, security, case isolation, evidence
  integrity, accessibility, resource bounds, recovery, and meaningful tests.
- Preserve unrelated work. Do not change implementation merely to make stale
  documentation true.

Before handoff, invoke the shared Agentics
[`simplicity_reviewer`](https://github.com/aaronspindler/agentics) after relevant
validation. Supply task requirements and the final scoped diff, including new
files and excluding unrelated changes. Wait for findings, resolve material issues,
and rerun affected checks and review after substantive fixes. Report remaining
findings or inability to run it. The reviewer returns findings without invoking
another reviewer. See [setup and invocation](docs/README.md#simplicity-review).

## Architecture and ownership

- Maintain one modular Django codebase for self-hosted and operator-hosted use.
  Use PostgreSQL locally, in hosted deployments, and for database integration
  tests. Local operation must not require a hosted-service subscription.
- Use current stable Python/Django releases when scaffolding. Keep runtime pins,
  dependency metadata, lockfiles, CI, and container images aligned. Verify upgrades
  against official compatibility documentation; do not default to prereleases.
  Current versions and migration constraints belong in [architecture](docs/architecture.md).
- Keep project configuration in `config/`: `config.settings`, `config.urls`,
  `config.wsgi.application`, and `config.asgi.application`. Align management and
  deployment entry points with these paths.
- Every Pandect model uses
  `models.UUIDField(primary_key=True, default=uuid4, editable=False)`, inherited
  from an abstract domain base where appropriate. Leave Django/allauth internal
  models and generated join tables on upstream schemas.
- Use `accounts.User` and django-allauth in both deployment modes. Reference users
  through `settings.AUTH_USER_MODEL` or `get_user_model()`.
- Use Django templates and progressive enhancement for the primary interface;
  reserve rich components for complex interactions. Authorization and domain rules
  belong in the backend.
- Keep a reproducible Docker Compose environment and documented configuration,
  migrations, persistent storage, health checks, upgrades, backup, and recovery.
  Never reset or migrate runtime data incidentally or from application startup.
  The pre-release migration baseline requires a fresh PostgreSQL database and
  queue; it provides no legacy data migration or compatibility aliases.

Keep Django apps at the repository root, with templates and static assets inside
their owning apps:

| Owner | Responsibility |
| --- | --- |
| `core` | Shared evidence foundations, evidence management, analysis lifecycle/orchestration, inference integration, and worker diagnostics. `core.evidence` and `core.analysis` are subpackages, not apps. Disable global queue administration: operational task records have no case authorization. |
| `text`, `image`, `audio`, `document`, `geo`, `phone`, `people`, `vehicle`, `socialmedia` | Concrete models, forms, specialized views, processors, assets, and tests. Geo owns locations, POIs, maps, and vehicle-location imports; phone owns messages, calls, and communication context. |
| `video` | Placeholder only; no implemented upload or analysis capability. |
| `accounts` | User model and account/allauth overrides. |
| `marketing`, `ui` | Public frontend and shared small design/brand primitives respectively. |

Core contracts must not import domain registries. Organize concrete analysis in
the owning app's `analysis/` package under the implemented categories:
`deterministic`, `metadata`, `ai` (including local OCR), and `external` (service
lookups, including private geography). These are code boundaries, not product
choices or processing-location labels. Compose mixed operations in a small domain
`workflows.py` module. Do not recreate repository-wide template or static roots.

## Case authorization and evidence integrity

- `cases.Case` is the investigation and access boundary. Each case has exactly one
  required owner; separate memberships allow at most one viewer/editor role per
  user and case. Invitations and membership changes never transfer ownership.
  Do not replace this with organization/workspace ownership.
- Owners invite/remove collaborators, assign roles, and delete cases. Viewers
  inspect material; editors also add evidence and edit investigative work.
  Ordinary investigators do not require staff access. Pandect owns invitations
  and case authorization; allauth supplies authentication.
- Enforce current case access server-side for every request, background operation,
  search, graph, download, and export. Check referenced objects belong to that
  case. Revocation must affect subsequent work. Self-hosting, UUID possession,
  hidden controls, and an earlier permission check do not grant access.
- Every evidence record requires a case. Preserve case boundaries in files,
  artifacts, indexes, caches, analysis, and exports. Global search may include
  only accessible cases.
- Preserve originals, source provenance, integrity metadata, processing versions,
  and history. Never silently replace originals or conflate source observations,
  extraction, analyst assertions, and AI suggestions. Preserve uncertainty and
  conflicts; identifiers, similarities, links, and scores do not prove identity,
  ownership, association, causation, or truth.
- Owners/editors archive or restore evidence. Individual deletion requires a
  superuser with current case access, explicit confirmation bound to a complete
  inventory, and post-commit file cleanup. Source deletion includes imported
  records and dependent artifacts; inventory changes require review again.
  Ordinary investigators retain evidence until owner-confirmed whole-case deletion.
- People and vehicles belong to one case: owners/editors add/edit, viewers inspect,
  and only owners delete. They are independent of login accounts. Retained
  person/vehicle evidence links protect individual entity deletion; whole-case
  deletion removes both. Communication parties must belong to the same case.
  Append retained call material with separate attribution/checksums; never replace
  earlier content.
- Treat uploads, external content, and model output as untrusted. Validate inputs,
  bound resources, and isolate risky parsers. Use synthetic or sanitized fixtures;
  never commit private evidence, runtime databases, credentials, or sensitive logs.

## Processing and AI

- Run lengthy ingestion and analysis in background jobs with visible progress,
  bounded retries, recoverable failures, and atomic publication. Retries must not
  duplicate evidence or present partial results as complete. The PostgreSQL queue
  supplies neither case authorization, automatic retries, nor stale-task recovery.
- Queue bounded local checks for eligible new investigator evidence. Keep LLM
  analysis, transcription, context-dependent analysis, and bulk import review
  deliberate. Preserve explicit timezone/vehicle interpretation and source-row
  provenance in vehicle imports.
- Printed-English image OCR uses local Tesseract in the bounded worker parser.
  No remote OCR or inference fallback; handwriting and mixed-text profiles remain
  unsupported.
- Default other supported inference to explicitly configured operator-controlled
  local/private-LAN runtimes through the shared backend. Pin model artifacts and
  runtime identities, retain execution provenance, and fail recoverably on drift.
  Bound inference separately from file parsers. Never silently fall back to cloud.
- External AI requires explicit selection and approval through Cloudflare AI
  Gateway. No direct-provider bypass or browser credentials. Operators supply
  credentials outside source control; distinguish gateway and upstream
  authentication. Keep provider/model configuration behind the integration boundary.
- Explain external data transfer where approval is requested, minimize payloads,
  and deliberately configure logging/retention. Do not log evidence payloads or
  secrets. Associate requests/results with their case and operation; provide usage
  accounting, limits, timeouts, bounded retries, and recoverable provider failures.
- Label model suggestions and preserve provenance. Never silently verify them or
  execute instructions from imported material/output. Non-AI work must remain
  usable without AI credentials. Mock inference in routine tests.

## Task-specific reading

Use the [documentation index](docs/README.md) for the full collection. Read the
relevant owner before extending these areas:

| Change | Required documentation |
| --- | --- |
| Setup, commands, validation | [Development](docs/development.md), [validation](docs/validation.md) |
| Case roles, invitations, admin | [Cases](docs/cases.md) |
| People or vehicles | [People](docs/people.md), [vehicles](docs/vehicles.md) |
| Evidence, original storage, communications, deletion | [Evidence](docs/evidence.md) |
| Analysis, reports, review, Gateway | [Analysis](docs/analysis.md) |
| Local routing, runtime limits, model qualification | [Local analysis](docs/local-analysis.md) |
| Tasks, atomic enqueueing, worker logs/recovery | [Background tasks](docs/background-tasks.md) |
| Vehicle CSV parsing/publication/retries | [Vehicle imports](docs/vehicle-location-imports.md) |
| Upload limits, mail, static delivery, logging, storage, deployment | [Operations](docs/operations.md) |

## Validation and handoff

- Use [documented commands](docs/validation.md) and keep local checks aligned with
  CI. Include migrations for model changes; use transactions and constraints to
  enforce integrity. Validate setup/migrations against PostgreSQL and exercise
  Compose startup/recovery for infrastructure changes.
- Cover meaningful behavior and failure paths, current owner/editor/viewer roles,
  unrelated/revoked users, cross-case references, and private file access. For
  processing changes, test mocked inference failures, retries, and partial failures.
- For visible changes, inspect desktop/mobile layouts, keyboard behavior, and
  accessibility, and capture visual evidence as specified in DESIGN.md. Manual
  screen-reader testing or system accessibility changes require an explicit user
  request and must not block handoff.
- For documentation-only changes, check accuracy, links, structure, and whitespace;
  do not add application tests that restate prose.
- Report changes, exact checks/results, and material limits. Distinguish local
  validation, CI, and live deployment/provider evidence. Validation does not
  authorize deployment or deletion of live data.
