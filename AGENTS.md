# Pandect contributor guidance

## Project context

Pandect is a local-first, self-hosted investigation application for one trusted operator. The foundation architecture and its explicit non-goals are canonical in [`docs/architecture/0001-foundation.md`](docs/architecture/0001-foundation.md). Read that document, the relevant issue, existing code, and tests before changing the repository.

## Core engineering principles

- Do not over-engineer. Prefer the simplest implementation that completely and correctly satisfies the current requirement.
- Prefer code that is easy to read, debug, test, and explain over clever, highly abstract, or excessively generic code.
- Follow established best practices for Django, Python, HTMX, SQLite, Huey, Docker Compose, testing, security, and dependency management.
- Inspect and reuse suitable repository patterns before adding conventions. Improve a pattern only for a concrete reason, and document that reason instead of creating a parallel approach silently.
- Keep changes focused. Avoid speculative abstractions, internal platforms, dependency-injection systems, generic frameworks, and configuration without a current use case.
- Extract shared abstractions from repeated concrete needs, not guessed future requirements. Keep modules cohesive, interfaces narrow, dependencies explicit, and side effects visible.
- Prefer composition and small testable units over deep inheritance or hidden framework behaviour.

## Local-first, privacy, and security constraints

- Treat Pandect as software that runs on the operator's machine, not as a cloud-hosted or multi-tenant SaaS product.
- Prefer local processing, local persistence, and locally served assets. Do not make cloud services, managed databases or queues, object storage, serverless functions, Kubernetes, remote observability, or third-party credentials default requirements.
- Preserve the loopback-only, trusted-machine boundary until an issue explicitly changes the security model. Do not add authentication, tenancy, roles, telemetry, analytics, remote logging, or automatic uploads by assumption.
- Use remote processing only when the capability cannot reasonably run locally or a configured source inherently requires a network API. Keep it optional where practical and document why it is needed, what data leaves the machine, who receives it, credential, cost, privacy, retention, and availability implications, and failure behaviour.
- Handle failures explicitly with actionable messages that do not reveal secrets or sensitive collected data. Never commit credentials, investigation data, exports, sessions, or machine-specific state.

## Architecture and extensibility

- Preserve clear boundaries among platform code, investigation-domain code, and source-specific integrations. Foundation changes must not invent domain models or source behaviour.
- Design a real source integration so another source can be added without modifying unrelated integrations. Isolate source authentication, collection, normalization, rate limits, and failure handling where practical.
- Use simple, explicit registration or wiring when it becomes necessary. Do not build a generic plugin framework before multiple real integrations establish repeated needs.
- Document extension points once they exist, including the minimum implementation, test, and registration steps.
- Ensure new code remains extensible without broad rewrites of unrelated functionality.

## Evidence integrity

- Preserve forensic integrity wherever practical. Treat raw source material as the canonical record; do not mutate, overwrite, or discard it unless an explicit requirement authorizes that operation.
- Store derived, normalized, enriched, and indexed representations separately from originals, and keep every derived artifact traceable to its source.
- Preserve timestamps, provenance, and source metadata alongside collected artifacts.
- Make normalization and enrichment reproducible and deterministic where practical. Avoid destructive processing that would prevent later verification, reinterpretation, or re-analysis.

## Testing, documentation, and validation

- Add or update focused tests for meaningful behaviour changes. Keep tests hermetic and independent of external credentials and services.
- Update documentation when setup, operation, configuration, extension points, or security boundaries change.
- Keep dependencies minimal and justify material additions. Use the locked project workflow and repository commands rather than introducing alternate tooling for the same task.
- Leave the repository with `make check` passing. When Docker or another required host capability is unavailable, run every safe local check and state the unverified boundary clearly.

## Strongly discouraged approaches

Do not add public network exposure, hosted defaults, silent data transfer, destructive evidence transformation, unrelated refactors, a single-page frontend or Node.js build pipeline, Redis or PostgreSQL as default infrastructure, or nested `AGENTS.md` files without a concrete directory-specific need.
