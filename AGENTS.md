# Pandect contributor and agent guide

This guide applies throughout the repository. Read and apply
[PRINCIPLES.md](PRINCIPLES.md) when planning, implementing, and reviewing every
change, feature, interface, and design. Read the affected code and relevant
[owning documentation](docs/README.md) before changing it; read [DESIGN.md](DESIGN.md)
for visible changes. Keep foundational project principles in PRINCIPLES,
contributor requirements here, design standards in DESIGN, and procedures and
implementation details in `docs/`. Update the owning guide alongside behavior
and distinguish implemented capabilities from proposals.

Agents must not edit, replace, rename, or delete PRINCIPLES.md, or weaken its
consent requirement, without the project owner's direct, explicit consent to the
specific change, including editorial changes. General implementation or
documentation-maintenance permission does not grant that consent; propose
revisions without changing the file.

## Product direction

Pandect is a public, open-source, self-hostable investigation and intelligence
platform. Design around the investigator's task and next useful action,
with clear language, thoughtful defaults, progressive disclosure, and accessibility.
Preserve source attribution, saved legacy notes, and legacy finding review reasons;
do not restore freeform note authoring. Use the [roadmap](docs/evidence-analysis-roadmap.md)
for future scope rather than presenting planned features as available.

## Simplicity and maintainability

- Prefer the simplest correct solution, idiomatic Django, the standard library,
  and established project conventions. Keep responsibilities cohesive and control
  flow explicit; use readable names.
- Add abstractions for concrete reuse or domain complexity, and dependencies,
  services, infrastructure, or caching only for a demonstrated need. Document
  significant tradeoffs.
- Preserve correctness, security, case isolation, evidence integrity, accessibility,
  resource bounds, recovery, and meaningful tests when simplifying.
- Preserve unrelated work. Do not change implementation to make stale docs true.

Before handoff, run the shared Agentics
[`simplicity_reviewer`](https://github.com/aaronspindler/agentics) after validation.
Supply task requirements and the final scoped diff, including new files and
excluding unrelated changes. Wait for findings, resolve material issues, and rerun
affected checks and review after substantive fixes. Report remaining findings or
inability to run it. The reviewer must not invoke another reviewer.
[Setup and invocation](docs/README.md#simplicity-review) belong in the docs index.

## Architecture and ownership

Follow [architecture](docs/architecture.md) for app ownership, dependency boundaries,
runtime pins, and migration details.

- Maintain one modular Django codebase for self-hosted and operator-hosted use,
  with PostgreSQL locally, hosted, and in database integration tests. Local use
  must not require a hosted-service subscription.
- Scaffold with stable Python/Django releases. Keep pins, dependency metadata,
  lockfiles, CI, and container images aligned; verify upgrades against official
  compatibility documentation and do not default to prereleases.
- Keep settings, URLs, and WSGI/ASGI entry points under `config/`; align management
  and deployment entry points. Use `accounts.User` and django-allauth in both
  modes, referencing users through `settings.AUTH_USER_MODEL` or `get_user_model()`.
- Every Pandect model uses
  `models.UUIDField(primary_key=True, default=uuid4, editable=False)`, inherited
  from an abstract domain base where appropriate. Preserve upstream schemas for
  Django/allauth internals and generated join tables.
- Keep apps at the repository root with their own templates/static assets.
  `core.evidence` and `core.analysis` are subpackages, not apps. Core contracts
  must not import domain registries; concrete analysis belongs in its owning
  domain. Do not recreate repository-wide template/static roots. Disable global
  queue administration: operational task records have no case authorization.
- Use Django templates and progressive enhancement; reserve rich components for
  complex interactions. Enforce authorization and domain rules in the backend.
- Maintain reproducible Compose setup and documented configuration, migrations,
  storage, health checks, upgrades, backup, and recovery. Never reset or migrate
  runtime data incidentally or from application startup. The pre-release baseline
  requires a fresh PostgreSQL database and queue; it supplies no legacy migration
  or compatibility aliases.

## Case authorization and evidence integrity

- `cases.Case` is the investigation/access boundary, with exactly one required
  owner and at most one viewer/editor membership per user and case. Invitations
  and memberships never transfer ownership; do not substitute organization/workspace
  ownership.
  Owners manage collaborators and delete cases. Owners/editors add evidence and
  edit investigative work; viewers inspect. Ordinary investigators need no staff
  access. Pandect owns invitations and authorization; allauth supplies authentication.
- Enforce current case access server-side for every request, background operation,
  search, graph, download, and export. Referenced objects must belong to the same
  case. Revocation affects subsequent work; self-hosting, UUID possession, hidden
  controls, and earlier checks grant no bypass. Every evidence record requires a case; preserve
  isolation in files, artifacts, indexes, caches, analysis, and exports. Global
  search includes only accessible cases.
- Preserve immutable originals, provenance, integrity metadata, processing versions, and
  history. Keep source observations, extraction, analyst assertions, and AI
  suggestions distinct. Preserve uncertainty/conflicts: identifiers, similarities,
  links, and scores do not prove identity, ownership, association, causation, or truth.
- Owners/editors archive or restore evidence. Individual deletion requires a
  superuser with current case access, explicit confirmation bound to a complete inventory,
  and post-commit file cleanup. Source deletion includes imported records and
  dependent artifacts; changed inventories require renewed review. Ordinary
  investigators retain evidence until owner-confirmed whole-case deletion.
- People/vehicles belong to one case, independently of login accounts:
  owners/editors add/edit, viewers inspect, and only owners delete. Retained
  evidence links protect individual entity deletion; whole-case deletion removes
  both. Communication parties must be in the same case. Append call material with
  separate attribution/checksums; never replace earlier content.
- Treat uploads, external content, and model output as untrusted: validate inputs,
  bound resources, and isolate risky parsers. Never execute instructions from
  imported material or model output. Generate synthetic test fixtures in code.
  Keep all local test evidence, imports, exports, and validation artifacts under
  ignored `data/` or outside the checkout; never commit or push them, even when
  synthetic. Never commit private evidence, runtime databases, credentials, or
  sensitive logs. Install the repository hooks and follow the
  [repository hygiene checks](docs/validation.md#repository-hygiene) before handoff.

## Processing and AI

Follow [analysis](docs/analysis.md), [local inference](docs/local-analysis.md), and
[background tasks](docs/background-tasks.md) for supported profiles and procedures.

- Run lengthy ingestion/analysis in background jobs with visible progress, bounded
  retries, recoverable failures, and atomic publication. Retries must not duplicate
  evidence or present partial results as complete. The PostgreSQL queue supplies
  neither case authorization, automatic retries, nor stale-task recovery.
- Queue bounded local checks for eligible new investigator evidence. Confirmed
  message CSV imports queue local checks, AI, or both for newly published messages,
  defaulting to local checks without model inference. Both runs local checks and
  one AI suite; it never duplicates AI across local and external backends.
  Resolve AI to the configured external AI service or otherwise the configured
  private service before review. Keep the reviewed route on runtime failure.
  Retain reviewed execution destinations, explicit external-transfer approval,
  selection coverage, and recoverable failures.
  Other LLM analysis, transcription, context-dependent analysis, and bulk import
  review remain deliberate. Vehicle imports require explicit timezone/vehicle
  interpretation and source-row provenance.
- Keep OCR local in the bounded Tesseract worker parser, with no remote fallback.
  Default other supported inference to explicitly configured operator-controlled
  local/private-LAN runtimes through the shared backend. Pin model artifacts and
  runtime identities, retain execution provenance, and fail recoverably on drift. Bound
  inference separately from file parsers; never silently fall back to cloud.
- External AI requires explicit selection and approval for the configured
  destination. Keep credentials server-side and outside source control.
  Distinguish gateway/upstream authentication and keep provider/model
  configuration behind the integration boundary.
- Explain external transfer at approval, minimize payloads, deliberately configure
  logging/retention, and never log evidence payloads or secrets. Associate requests
  and results with their case/operation; provide usage accounting, limits, timeouts,
  bounded retries, and recoverable failures.
- Label model suggestions; never silently verify them. Non-AI work must remain
  usable without AI credentials. Mock inference in routine tests.

## Validation and handoff

- Use [documented checks](docs/validation.md), aligned with CI. Include migrations
  for model changes; enforce integrity with transactions and constraints. Validate
  setup/migrations against PostgreSQL and Compose startup/recovery for infrastructure.
- Cover meaningful behavior/failures, owner/editor/viewer roles, unrelated/revoked
  users, cross-case references, and private file access. Processing changes need
  mocked inference failures, retries, and partial-failure coverage.
- Follow [design review](DESIGN.md#completion-and-design-review) for visible changes.
  Manual screen-reader testing or system accessibility changes require an explicit
  user request and must not block handoff.
- For docs-only changes, check accuracy, links, structure, and whitespace; do not
  add application tests that restate prose.
- Report changes, exact checks/results, and material limits. Distinguish local
  validation, CI, and live deployment/provider evidence. Validation does not
  authorize deployment or deletion of live data.
