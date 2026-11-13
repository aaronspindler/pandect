# Pandect code review — 11 November 2026

Historical review of the dated snapshot. Paths, counts, and validation results
below describe that review, not the current checkout. Use [validation](validation.md)
for current commands and [architecture](architecture.md) for current ownership.

The review covered first-party Django apps, worker and inference boundaries,
investigator and public templates and scripts, tests, Compose, and CI/CD.
Vendored code was reviewed at its integration points. The starting worktree
already contained an analysis UI refactor. The remediation diff was measured
against that worktree to isolate this review’s changes.

## Findings and corrections

| Area | Finding | Correction |
| --- | --- | --- |
| [Product authentication](../config/access.py) and [private logs](../config/privacy.py) | Sign-in copied invitation tokens into the login URL, routine log redaction missed account credential paths, and enhanced requests could receive JSON when their Accept header explicitly excluded it. | Resume invitations through the session, redact credential-bearing paths in application and access logs, and use Django’s media-type negotiation. |
| [Case invitations](../cases/views.py) | Invitation previews acquired write locks and set response headers that the privacy middleware replaced. Duplicate-access validation loaded all collaborator IDs into Python. | Lock on acceptance, let the middleware set response privacy, and check both account email sources with a database existence query. |
| [Evidence editing and deletion](../core/evidence/views.py) | Metadata editing held write locks on GET and during rendering. Deletion scanned the inventory twice at confirmation and repeatedly scanned dependent runs. | Hold write locks during edit validation and writes, reuse one locked deletion plan per confirmation, and traverse the dependency graph once. |
| [Communication readers](../phone/presentation.py) | Reply and thread cards used a weaker case filter than the normal evidence reader, loaded full message rows, and queried related material on tabs that did not display it. | Apply the reader’s case rules and load the displayed fields on the Original tab. |
| [Upload queue](../core/static/core/evidence.js) | Removing the last file left the single-upload form fields hidden, with focus on a removed control. Queue edits could also leave a stale navigation warning. | Restore the original fields, return focus to the file picker, and use queue state for unsaved entry edits. |
| [Parser process boundary](../core/parsers.py) | Parser output was collected into memory before its byte limit was applied. | Redirect output to a private temporary file subject to the worker’s file-size limit and read a bounded prefix before decoding. |
| [Analysis publication](../core/analysis/services.py) | Invalid result shapes could fail before validation. Passage projection repeatedly searched source objects and inserted rows individually while holding the case lock. | Validate shape before publication and batch searchable passages with their source attribution. |
| [Analysis submission and reviews](../core/analysis/services.py) | Concurrent cross-case reuse of a submission identifier surfaced a database error. Geocode review resolution could follow inconsistent related rows across cases. | Preserve the normal submission error across concurrent requests and scope review lookup to the result case. |
| [Geographic analysis](../geo/analysis/external/geocoding.py) | Geocoding initialized a client even when every selected source was ineligible, and reported that result as partial. Two consent tests inherited the operator’s locality setting. | Initialize lookup only for eligible sources, return insufficient input when all are skipped, and set test locality explicitly. |
| [Vehicle CSV import](../geo/vehicle_import_jobs.py) | Duplicate flags could stay set after a matching observation was deleted. Duplicate lookup transferred repeated hashes, the upload path hashed the same file twice, and review loaded the vehicle choices for each mapping. | Recompute flags before publication, fetch distinct matches, reuse the upload fingerprint and rollback path, and load vehicle choices once. |
| [Map estimates](../geo/map_geocoding.py) | The map queried item reviews separately for every visible geocoding run. | Fetch reviews once for the visible page. |
| [Conversation import](../phone/analysis/deterministic/imports.py) | Valueless HTML attributes could break parsing, oversized timestamps could reach publication, and equivalent UUID spellings could disagree at confirmation. Valid message bodies were held in three result fields and could exhaust the result limit. | Normalize source and mapping fields, publish from the retained finding text, validate same-case links before loading retained bodies, and record the new parser and method version. |
| [Local inference](../inference/server.py) | A replaced file could inherit a digest computed from its predecessor, and the runtime kept a native engine alive after its verified model disappeared. | Check file identity during verification and stop the engine when its model changes or becomes unavailable. |
| [Deployment verification](../scripts/deploy-dokploy.py) | An incomplete transport response escaped normal error handling. Empty or non-object health responses aborted polling before ingress could become ready. | Convert transport failures into bounded deployment errors and require a valid readiness object before counting a probe as healthy. |

The development guide now identifies the operations that already use the
[background worker](background-tasks.md). The changes require no schema migration.
Finish queued iMessage imports before upgrading or start a fresh run with the same
source and settings afterward. See [import provenance](analysis-communications.md#retained-imessage-html-import).

## Local validation

- `make check` passed on the final diff with 909 tests against a disposable
  PostgreSQL database, plus Ruff, lockfile, migration, and Compose checks.
- `make browser-check` passed 40 tests in Chromium and WebKit, including keyboard
  removal of the last queued file at desktop and mobile widths. After the
  backend-only review follow-ups, the focused import and result suites passed
  30 tests.
- The final `make compose-smoke` passed fresh startup, queue execution and
  restart, retained originals, and static delivery in a disposable project.
- `make security-check` found no known vulnerabilities in the locked Python
  dependency graph. The final simplicity review found no remaining material
  issues in the scoped diff.
