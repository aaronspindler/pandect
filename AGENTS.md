# Pandect contributor and agent guide

This guide applies throughout the repository. Read it before making changes, then
read the relevant documentation in `docs/` and inspect the affected code. Keep this
file focused on durable project requirements; put detailed designs, procedures,
and explanations in `docs/`.

## Product direction

Pandect is an open-source investigation and intelligence platform. It must combine
comprehensive analysis, connection discovery, and investigation tools with an
exceptionally approachable experience: the Apple of OSINT projects.

The product direction includes case management, evidence ingestion, search, entity
and relationship analysis, timelines, maps, annotations, and reporting. These are
capabilities to build incrementally, not claims about what already exists.

- Design around an investigator's task and next useful action.
- Lead routine workflows with evidence and analysis. Keep optional source metadata,
  descriptions, and notes available through progressive disclosure.
- Use clear language, consistent navigation, thoughtful defaults, and progressive
  disclosure. Make advanced capabilities discoverable without overwhelming routine
  work.
- Provide useful empty, loading, progress, success, and error states. Explain how
  users can recover from failures.
- Maintain responsive layouts, keyboard access, visible focus, semantic controls,
  readable contrast, and accessible alternatives to complex visualizations.
- Make sources, uncertainty, and the distinction between observations and
  conclusions understandable within the investigation workflow.
- Treat usability and visual polish as part of feature completion.

Read [DESIGN.md](DESIGN.md) before designing or changing visible interfaces. Keep
detailed screen specifications and implementation plans in `docs/`, and distinguish
design targets from implemented capabilities.

## Simplicity and maintainability

Keep architecture and code simple, maintainable, and consistent with established
best practices and standards in every change. Do not overengineer the project.

- Choose the simplest correct solution that satisfies current requirements. Avoid
  speculative features, premature optimization, and flexibility for hypothetical
  future needs.
- Prefer idiomatic Django, standard-library capabilities, and established project
  conventions. Follow documented Python, Django, PostgreSQL, and web best practices
  instead of inventing custom frameworks or replacing proven built-in behavior.
- Keep modules cohesive, responsibilities clear, and control flow explicit. Favor
  readable names and straightforward code that contributors can understand, test,
  and change without tracing unnecessary layers of indirection.
- Introduce abstractions when concrete reuse or domain complexity justifies them.
  Avoid generic frameworks, redundant wrappers, and configuration options without
  a demonstrated use case.
- Add dependencies, services, infrastructure, and caching only when a concrete
  requirement or measured bottleneck warrants their maintenance and operational
  cost. Document significant tradeoffs in `docs/`.
- Simplicity must preserve correctness, security, case isolation, evidence
  integrity, accessibility, and meaningful tests. Small amounts of code are not a
  substitute for clear behavior and sound engineering.

Before handing changes back to the user, the implementing agent must invoke
the shared Agentics [`simplicity_reviewer`](https://github.com/aaronspindler/agentics)
after implementation and relevant validation. Give it the task requirements and
final scoped diff, including new files, while excluding unrelated existing changes. Wait for its
findings, resolve material issues within scope, and rerun affected checks and
review after substantive fixes. Report any remaining findings or inability to run
the review honestly. The reviewer returns findings directly and does not invoke
another reviewer. See [the review workflow](docs/README.md#simplicity-review)
for invocation and configuration details.

## Architecture and deployment

- Use the latest stable releases of Python and Django when scaffolding the
  application and keep them current. Verify release versions and compatibility
  against official documentation; do not use prereleases as the default.
- Build a modular Django application with PostgreSQL as the database in both local
  and hosted deployments. Use PostgreSQL for database integration tests as well.
- Declare UUID primary keys on every Pandect model with
  `models.UUIDField(primary_key=True, default=uuid4, editable=False)`. Inherit this
  field in abstract domain bases where appropriate. Keep Django/allauth internal
  models and automatically generated join tables on their upstream schemas.
- Name the Django project configuration package `config` and place it in
  `config/`. Create it with `python -m django startproject config .` from the
  repository root. Use `config.settings` for `DJANGO_SETTINGS_MODULE`,
  `config.urls` for `ROOT_URLCONF`, `config.wsgi.application` for
  `WSGI_APPLICATION`, and `config.asgi.application` for `ASGI_APPLICATION`.
  Keep `manage.py`, `config/wsgi.py`, `config/asgi.py`, and deployment server
  entry points aligned with these module paths.
- Maintain one application codebase and domain model for self-hosting and the
  operator-hosted service. Configure deployment differences rather than creating
  separate product implementations.
- Use Django templates and progressive enhancement for the primary interface.
  Introduce focused rich components for graphs, timelines, maps, and similarly
  complex interactions. Keep authorization and domain rules in the backend.
- Provide a reproducible local Docker Compose environment with the application,
  PostgreSQL, and any required background services. Document configuration,
  migrations, persistent storage, startup, health checks, and shutdown.
- Support documented deployment, upgrades, backup, and recovery. Local operation
  must not require a subscription to the hosted Pandect service.
- Put lengthy ingestion and analysis in background jobs with visible progress,
  bounded retries, and recoverable failures. Retried work must not duplicate
  evidence or publish partial results as complete.
- Use configurable storage suitable for local files and hosted operation. Evidence
  downloads and derived artifacts must remain subject to case authorization.
- Choose the queue implementation and specialized libraries during implementation.
  Record significant decisions in `docs/` and keep Python and Django versions,
  runtime pins, dependency metadata, lockfiles, CI, and container images aligned.

## Identity, ownership, and collaboration

Use a custom Django `User` model and django-allauth in both deployment modes.
Configure `AUTH_USER_MODEL` before creating initial migrations. Reference users
through `settings.AUTH_USER_MODEL` or `get_user_model()` as appropriate; do not
couple application code to Django's built-in concrete user model.

`Case` is the investigation and access boundary:

- Every case has exactly one owner through a required user foreign key.
- Owners can invite users, remove collaborators, assign viewer or editor access,
  and delete the case.
- Viewers can inspect case material. Editors can additionally add evidence and
  edit investigative work.
- Store collaboration membership separately from ownership. A user has at most
  one membership in a given case. Invitations and membership changes must never
  implicitly transfer ownership or create another owner.
- Implement case invitations and membership in Pandect, integrated with allauth
  authentication. Do not assume allauth supplies invitation handling or case
  authorization.
- Apply permissions server-side to every relevant request and operation, including
  searches, graphs, file downloads, exports, and background jobs. Check access to
  the case and ensure referenced objects belong to it.
- Removing access must affect subsequent operations. Background processing must
  account for permission changes rather than relying only on an earlier UI check.
- Self-hosting does not bypass authentication or case permissions. Possession of
  an object identifier and hidden UI controls are not authorization.

Do not substitute an organization or workspace ownership model for this case-based
model without an explicit change in product requirements.

## Evidence and investigation integrity

- Every evidence record must have a required relationship to a case. Files,
  extracted content, derived artifacts, and analysis results must remain traceable
  to their evidence and case; never create an unscoped pool of investigation data.
- Preserve original evidence and capture source provenance and integrity metadata.
  Processing, annotations, and corrections must not silently overwrite originals.
- Keep source observations, extracted or inferred information, analyst assertions,
  and AI-generated suggestions distinguishable and traceable. Record the relevant
  processing versions and history needed to explain a result.
- A shared identifier, similar image, graph connection, or model score is not by
  itself proof of identity, ownership, causation, or truth. Preserve uncertainty
  and conflicting evidence rather than silently resolving them.
- Preserve case boundaries in indexes, caches, processing, and exports as well as
  database queries. Global search must include only cases the user may access.
- Treat uploaded files and external content as untrusted input. Bound processing
  resources, validate inputs, and isolate risky parsers as appropriate.
- Use synthetic or appropriately sanitized fixtures and demonstrations. Do not
  commit private investigation material, uploaded files, runtime databases,
  credentials, or sensitive logs.

## ML and LLM processing

Run printed-English image text extraction locally
with Tesseract in the bounded worker parser. Never send images to an external
service for OCR or fall back to remote inference. Keep handwriting and mixed-text
profiles unsupported until a suitable local implementation is delivered.

Default other supported ML/LLM inference to explicitly configured,
operator-controlled local or private-LAN runtimes through the shared backend
integration. Pin model artifacts and runtime identities, retain execution
provenance and fail recoverably on drift. Never fall back to an external service
automatically. Keep local inference separately bounded from file parsers.
External AI remains an explicitly selected and approved Cloudflare AI Gateway
route using API credentials. Do not bypass the gateway with direct provider calls
or place credentials in browser code. Read `docs/local-analysis.md` before changing
local routing, runtime operations, resource limits or model qualification.

- Credentials belong to the deployment operator: self-hosters configure their own
  gateway access, and the hosted service operator supplies hosted access.
- Keep credentials outside source control, using environment configuration or a
  suitable secret store. Document configuration with placeholders only.
- Make local/runtime and gateway configuration and model selection configurable. Keep provider and
  model details behind the integration boundary rather than scattering them
  through views and domain logic.
- Follow the current Cloudflare authentication requirements for the endpoint in
  use. Distinguish gateway authentication from any upstream provider credentials;
  do not assume one API key works for every endpoint or provider.
- Provide usage accounting and configurable limits, request timeouts, bounded
  retries, and understandable handling of authentication, quota, rate-limit, and
  provider failures. Associate requests and results with their case and operation.
- Explain when evidence is sent for external processing. Minimize submitted data
  and deliberately configure gateway logging and retention; do not assume gateway
  defaults protect evidence privacy. Avoid recording evidence payloads or secrets
  in routine application telemetry.
- Treat model output as untrusted derived information. Preserve provenance and
  label suggestions; do not silently promote them to verified evidence or execute
  instructions contained in imported material or model responses.
- Keep ordinary case management and non-AI investigation work usable when AI
  credentials are absent or the gateway is unavailable. Communicate the resulting
  limitation within affected AI features.
- Mock inference calls in routine automated tests. Do not make routine tests depend on paid
  inference, real credentials, or external availability.

## Documentation is part of delivery

`docs/` is the canonical, source-control-tracked home for project documentation.
Create and maintain it as implementation begins, and update relevant documentation
in the same change as the behavior it describes.

Documentation must grow alongside the project to cover:

- Product concepts, user workflows, case ownership, and collaboration permissions.
- Local setup, configuration, Docker Compose operation, and contributor workflows.
- Architecture, data relationships, integrations, and significant design decisions.
- Hosted deployment, migrations, upgrades, backup, restoration, and troubleshooting.
- Evidence handling, access control, and external AI processing configuration.

Keep instructions executable and examples free of real credentials or evidence.
Distinguish implemented capabilities from proposals and planned work. Maintain a
documentation entry point as the collection grows, and update `AGENTS.md` when
durable project requirements change.

## Engineering and validation

- Inspect the current implementation before changing it. Make focused changes,
  preserve unrelated work, and prefer clear Django conventions and maintainable
  modules over speculative abstractions.
- Include schema migrations with model changes and document compatibility or
  operational consequences. Use transactions and database constraints where they
  enforce domain integrity.
- Add meaningful behavior and failure-path coverage for implemented features.
  Test owners, viewers, editors, unrelated users, revoked access, and attempts to
  cross case boundaries, including direct object and file access.
- Validate initial setup and migrations against PostgreSQL. Exercise the Compose
  startup and recovery workflows when relevant infrastructure changes.
- Test AI integration failures and permission enforcement with mocked responses.
  Validate background retry and partial-failure behavior when changing processing.
- For visible UI changes, inspect desktop and mobile layouts and verify keyboard
  interaction and accessibility behavior. Capture relevant visual evidence.
  Manual screen-reader testing is optional during development and must not block
  completion or handoff. Run it or change system accessibility settings only when
  the user explicitly requests that check.
- Use the repository's documented validation commands once established. Keep CI
  and local checks aligned; do not invent commands or report unrun checks as passed.
- Report what changed, how it was verified, and any material limits. Distinguish
  local validation, CI results, and live deployment or provider evidence.
- Review documentation-only changes for accuracy, links, structure, and whitespace;
  do not add application tests that merely restate documentation.

## Rebuild context

The rebuild includes `config/`, `accounts.User`, django-allauth, PostgreSQL,
case/people/vehicle management, invitations and viewer/editor collaboration, retained
image/audio/document/text/location/person-location/vehicle-location/communication evidence
and private downloads,
scoped operator administration, uv dependency management, Docker Compose, and local/CI checks.
The investigator interface follows `DESIGN.md` and the refined three-pane design;
normal users do not need staff permissions.
Read `docs/development.md` and `docs/validation.md` for executable commands. The
compatible runtime versions and upgrade constraints are in `docs/architecture.md`.
Use `cases.Case` for future investigation and evidence relationships. See
`docs/cases.md` for implemented workflows, invitations, and product/admin
permissions. Bounded Asset Location CSV imports and case-scoped analysis are
implemented; read `docs/analysis.md` for supported extraction profiles, source
anchors, reports, background processing, Gateway configuration, and review
semantics. Broader catalogue features, graphs, unified timelines, and specialist
analysis remain planned.
The Django Tasks/PostgreSQL worker supports diagnostics, case analysis, and
vehicle-location CSV preparation/publication. Other existing product workflows
remain synchronous. Read
`docs/background-tasks.md` before adding tasks or changing worker operations,
atomic enqueueing, private task logging, or manual failure recovery. The queue
does not supply case authorization, automatic retries, or stale-task reclamation.
Read `docs/people.md` before extending person records or their admin permissions.
People belong to a single case; owners and editors can add/edit them, viewers can
inspect them, and only owners can delete them. Person records are independent of
login accounts and do not establish verified identity.
Read `docs/vehicles.md` before extending vehicle records or their administration.
Vehicles belong to a single case and follow the same roles as people. Reported
plates and VINs do not establish identity, ownership, or a person's association.
Read `docs/evidence.md` before extending evidence models, original storage, or
deletion cleanup. Original preservation is enforced through model validation and
saves; current product routes enforce case permissions and every future access
path must do so too. Owners/editors archive or restore individual evidence.
Superusers with current case access may explicitly confirm individual deletion
from evidence Details, including dependent analysis and retained files. The scoped
Django admin also permits this for active staff superusers. Keep individual
deletion disabled for ordinary investigators/staff. Superuser deletion of a source
document includes its imported observations/messages, import records, and dependent
analysis/files. Show the complete inventory and bind confirmation to it; changes
require another review. Preserve case isolation and post-commit file cleanup.
Owners may still confirm whole-case deletion.
Communication evidence requires same-case sender/recipient people. Its originals
are immutable; owners/editors may append retained call transcripts/audio with
independent attribution/checksums, never replace earlier content. Read
`docs/evidence.md` before extending these workflows or private attachments.
All evidence types include an optional Source name/citation. Location originals
retain an address or coordinate pair and optional observation time. Person-location
evidence also retains a same-case person link; it prevents individual deletion
of that person, while whole-case deletion removes both together.
Vehicle-location evidence retains a same-case vehicle link with equivalent
deletion protection. Read `docs/vehicle-location-imports.md` before changing the
bounded CSV parser, explicit timezone/vehicle interpretation, source-row provenance,
atomic completion, or duplicate-safe retries. Keep lengthy ingestion out of request
handlers and preserve the worker's bounded parsing, progress, and recovery.
Read `docs/operations.md` before changing upload limits, mail, static delivery,
private logging, storage, or deployment behavior.

The new requirements supersede historical SQLite and single-operator assumptions.
Do not delete or migrate existing runtime data as an incidental part of unrelated
work. No legacy data migration or project licensing decision is part of the
application foundation.

## Primary integration references

- [Django custom user models](https://docs.djangoproject.com/en/stable/topics/auth/customizing/)
- [django-allauth custom users and invitations](https://docs.allauth.org/en/latest/account/advanced.html)
- [Cloudflare AI Gateway authentication](https://developers.cloudflare.com/ai-gateway/configuration/authentication/)
- [Cloudflare AI Gateway logging](https://developers.cloudflare.com/ai-gateway/observability/logging/)
