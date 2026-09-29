# Contributing to Pandect

Small, focused contributions are welcome. For substantial behavior or
architecture changes, discuss the user need in an issue first. Use synthetic
reproductions: never submit investigation originals, databases, embeddings,
backups, tokens, or unredacted logs. Report suspected vulnerabilities through
the [private security process](../SECURITY.md), not a public issue or PR.

By intentionally submitting a contribution for inclusion, you provide it under
Pandect's [Apache-2.0 license](../LICENSE), unless explicitly agreed otherwise.
Only contribute material you have the right to share and preserve upstream
notices. No separate CLA or sign-off requirement is imposed. Treat other
contributors respectfully and keep review focused on behavior and evidence.

Read the relevant issue, the root [`AGENTS.md`](../AGENTS.md), and
the [current architecture](architecture/overview.md) and its linked ADRs before
changing Pandect. The root guidance is authoritative for engineering style,
local-first operation, evidence integrity, and validation.

Use a focused branch and PR for changes to `main`. Explain the operator need,
include a synthetic reproduction for bugs, and report exactly which checks
passed or could not run. Keep unrelated refactors out of the patch and resolve
review discussions before merging. CI checks are required; a passing workflow
does not replace reviewing evidence/privacy behavior. You do not need to use
an AI coding tool to contribute; `AGENTS.md` also records shared engineering
constraints, while this guide is the human workflow.

## Validation

Validation runs in the profile-gated Docker `tools` service, using Python and
development dependencies pinned by the project lockfile and image. Host Python
and `uv` are not required; do not substitute an independent dependency
workflow.

Run the focused tests while developing, then leave the repository with the full
validation passing:

```console
make test
make lint
make validate
make check
```

The narrower targets match the separately reported CI steps. `make validate`
runs Django's system check, detects missing migrations, validates the Compose
configuration, builds the runtime image, and imports its native media runtimes.
`make check` runs all three targets as the complete local pre-submit check.

Tests must be hermetic: use temporary state and eager Huey execution, with no
third-party credentials or network access. Test settings discard ambient
Cloudflare enablement and credentials, and the suite rejects real IP and DNS
access. Compose also starts every `tools` invocation with `network_mode: none`,
so pytest, real local-media checks, and the non-eager worker subprocess have no
IP network namespace even if a Python-level guard regresses. Image builds happen
before that container starts and may fetch only locked dependencies and OS
packages. Exercise HTTP adapters with an in-memory mock transport; do not
disable the suite-wide guard. Local subprocess worker tests remain supported
because they communicate through SQLite and process signals rather than the
network.
Commit intentional migrations with the model change that needs them. Keep
dependencies minimal and update the dependency declaration and lockfile
together rather than adding a second toolchain. Dependency updates are the one
workflow that needs the same `uv` version pinned in the Dockerfile
and used by CI's tools image; after editing `pyproject.toml`, regenerate
`uv.lock` with that version, review the diff, and finish with `make check`.
Locked installation rejects stale project metadata instead of ignoring it.

`make security-check` is a separate, explicitly network-enabled check: it audits
the lockfile, scans runtime and maintenance images for fixable high/critical vulnerabilities,
and scans Git history plus nonignored working files for secrets with redacted
output. It runs without application evidence or credentials. Its advisory
lookups/image downloads are not part of the hermetic-test claim. The scan needs
several GiB of temporary disk for image archives and the expanded vulnerability
database; the private disk cache is cleaned afterwards. Exceptions in
`security-exceptions.json` must name one advisory and scanner, owner,
justification, and an unexpired review deadline; a blanket ignore is not an
acceptable workaround. Never include a live secret in a suppression or report.

CI validates Linux amd64 and arm64. The release checklist additionally requires
real Compose recovery/isolation smoke checks and the documented large-media
capacity test. Do not infer those results from a unit-test pass.

## Adding a future Django app

Add an app only for a concrete, cohesive capability that does not belong in the
platform-only `pandect.core` app. Follow the existing `pandect/<app_name>`
package layout and explicit Django wiring rather than introducing a new
framework:

1. Give the app one clear domain responsibility and a narrow public interface.
2. Add it explicitly to Django settings and URL routing only where needed.
3. Namespace its templates and URLs; keep full-page navigation usable without
   JavaScript and use HTMX only for a justified fragment interaction.
4. Keep migrations, tests, tasks, and management commands with the capability
   they implement. Make external side effects visible and testable.
5. Preserve raw evidence as the canonical record. Store derived or normalized
   representations separately with provenance and source timestamps so they
   can be reproduced and traced back.
6. Update operating or extension documentation when the change adds setup,
   configuration, security, or registration steps.
7. Run `make check`.

Do not couple a new app to login, users, roles, ownership, permissions, or
tenancy. The current trusted-operator boundary has no application
authentication. Authentication and support for untrusted access require a
separate architecture and security decision; adding incidental user foreign
keys or login decorators does not provide that boundary.

Do not make application startup depend on cloud services, managed storage,
hosted queues, remote observability, or credentials for a future source. Local
processing and persistence remain the default.

## Templates and HTMX

Full pages extend `templates/base.html`. Put app-specific full templates under
`templates/<app_name>/` and fragments under
`templates/<app_name>/partials/`. Prefer an ordinary full-page response; when a
targeted partial update has a concrete benefit, the same view may select a
fragment for an `HX-Request` and retain a useful non-HTMX fallback.

Keep POST paths protected by Django CSRF handling. The base template supplies
the CSRF header for HTMX, while ordinary forms still include `{% csrf_token %}`.
Serve browser assets from the project-owned `static/` tree. Do not add a CDN,
Node.js pipeline, client state store, or SPA framework for a fragment update.

## Adding a future source

There is intentionally no generic source registry or plugin system yet. The
first real source should establish only the smallest concrete boundary its
requirements demonstrate. Keep its authentication, network client, collection,
normalization, rate-limit handling, retries, and failure reporting isolated so
another source can later be added without editing unrelated integrations.

When a source inherently requires a remote API, make configuration explicit
and avoid contacting it during imports, startup, health checks, or unrelated
workflows. Document why local processing is not feasible; exactly what data
leaves the machine and which service receives it; credential, cost, privacy,
retention, and availability implications; and behavior when the service is
unavailable. Keep the default application usable without those credentials.

Source tasks should have bounded timeouts and be idempotent where practical.
Retry only operations known to be safe. Store raw responses or collected
artifacts without destructive normalization, and link every derived record to
its raw source and provenance. Errors must be actionable without logging
credentials or sensitive collected content.

After repeated source implementations reveal a stable common need, extract a
small shared interface and document its minimum implementation, tests, and
explicit registration steps. Do not predict those requirements by building a
general plugin framework in advance.

## Adding a processing stage or local model

Processing is explicitly wired rather than plugin-driven. Add the stage choice,
item-type dependency, extractor, dispatcher entry, provenance schema, and tests
together. Parsers should accept fixed fixture output and return JSON-compatible
values; tasks receive only UUIDs. Keep expensive work outside SQLite
transactions and recheck the canonical input hash before appending output.

Every native tool, decoder, and model call must run through the media runner's
fixed operation interface, including probes, diagnostics, and validation of
derived media. There is no direct-execution fallback. Inside a job use argument
arrays, no shell, bounded output, and a hard timeout. Do not add arbitrary
executable/path/environment parameters to the protocol. Validate runner output
before the trusted orchestrator records it; recompute file hashes rather than
trusting a parser's claimed digest. Keep credentials, database access, consent
decisions, and evidence publication outside the runner. Errors must not retain
evidence, paths, arguments, or tool output. Use existing attempt-journaled
publication for derived media; never overwrite or delete the original.

Read the [model licensing inventory](model-licenses.md) before changing a model.
Model weights are not Python dependencies and may not be fetched at import,
startup, health checks, or task execution. Add a digest-qualified manifest
entry with source, exact revision, preprocessing version, platform
compatibility, and a defensible artifact-specific training-corpus provenance
limitation. Bind every model to the exact upstream license/NOTICE URL, digest,
and size so explicit provisioning archives the actual text under `models/legal/`
without replacing prior legal files. Core tests use fake adapters and may not
load or download weights or legal text; put real inference in the explicit
optional smoke target.

Remote adapters require a new architecture/privacy review. Bind consent to
recipient, model, prompt, exact source and derivative hashes, request count,
and transfer configuration; a change must invalidate it. Tests must assert that
no adapter call is possible without current item-specific consent and must stay
fully network-isolated.
