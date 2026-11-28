# Pandect code review — 11 November 2026

Historical snapshot: paths, counts, and results are dated. Use
[validation](../validation.md) and [architecture](../architecture.md) for current
commands and ownership.

Scope: first-party Django apps, worker/inference boundaries, templates/scripts,
tests, Compose, and CI/CD; vendored code at integration points. The remediation
diff used the starting worktree to exclude its existing analysis UI refactor.

## Findings and corrections

| Area | Finding | Correction |
| --- | --- | --- |
| [Product authentication](../../config/access.py) and [private logs](../../config/privacy.py) | Sign-in exposed invitation tokens in login URLs, redaction missed credential paths, and enhanced responses could ignore Accept exclusions. | Resume invitations through the session, redact credential-bearing paths in application and access logs, and use Django’s media-type negotiation. |
| [Case invitations](../../cases/views.py) | Previews acquired write locks and set headers replaced by privacy middleware. Duplicate-access checks loaded all collaborator IDs. | Lock on acceptance, let the middleware set response privacy, and check both account email sources with a database existence query. |
| [Evidence editing and deletion](https://github.com/aaronspindler/pandect/blob/b91baee27e679bd14978c3706f6d03998235ae4b/core/evidence/views.py) | Metadata GET/rendering held write locks. Deletion repeated inventory and dependent-run scans. | Hold write locks during edit validation and writes, reuse one locked deletion plan per confirmation, and traverse the dependency graph once. |
| [Communication readers](../../phone/presentation.py) | Reply/thread cards used weaker case filters, loaded full messages, and queried material on tabs that hid it. | Apply the reader’s case rules and load the displayed fields on the Original tab. |
| [Upload queue](../../core/static/core/evidence.js) | Removing the last file left the single-upload form fields hidden, with focus on a removed control. Queue edits could also leave a stale navigation warning. | Restore the original fields, return focus to the file picker, and use queue state for unsaved entry edits. |
| [Parser process boundary](../../core/parsers.py) | Parser output was collected into memory before its byte limit was applied. | Redirect output to a private temporary file subject to the worker’s file-size limit and read a bounded prefix before decoding. |
| [Analysis publication](../../core/analysis/services.py) | Invalid result shapes could fail before validation. Passage projection repeatedly searched source objects and inserted rows individually while holding the case lock. | Validate shape before publication and batch searchable passages with their source attribution. |
| [Analysis submission and reviews](../../core/analysis/services.py) | Concurrent cross-case reuse of a submission identifier surfaced a database error. Geocode review resolution could follow inconsistent related rows across cases. | Preserve the normal submission error across concurrent requests and scope review lookup to the result case. |
| [Geographic analysis](../../geo/analysis/external/geocoding.py) | Geocoding initialized a client even when every selected source was ineligible, and reported that result as partial. Two consent tests inherited the operator’s locality setting. | Initialize lookup only for eligible sources, return insufficient input when all are skipped, and set test locality explicitly. |
| [Vehicle CSV import](../../geo/vehicle_import_jobs.py) | Duplicate flags could stay set after a matching observation was deleted. Duplicate lookup transferred repeated hashes, the upload path hashed the same file twice, and review loaded the vehicle choices for each mapping. | Recompute flags before publication, fetch distinct matches, reuse the upload fingerprint and rollback path, and load vehicle choices once. |
| [Map estimates](../../geo/map_geocoding.py) | The map queried reviews per visible geocoding run. | Fetch reviews once for the visible page. |
| [Conversation import](../analysis-communications.md#message-csv-imports) | Valueless HTML attributes could break parsing, oversized timestamps could reach publication, and equivalent UUID spellings could disagree at confirmation. Valid message bodies were held in three result fields and could exhaust the result limit. | Normalize source and mapping fields, publish from the retained finding text, validate same-case links before loading retained bodies, and record the new parser and method version. |
| [Local inference](https://github.com/aaronspindler/pandect/blob/b91baee27e679bd14978c3706f6d03998235ae4b/inference/server.py) | Replacement files could inherit stale digests; native engines survived loss of their verified model. | Check file identity during verification and stop the engine when its model changes or becomes unavailable. |
| [Deployment verification](../../scripts/deploy-dokploy.py) | Incomplete transport responses escaped error handling; malformed health responses aborted readiness polling. | Convert transport failures into bounded deployment errors and require a valid readiness object before counting a probe as healthy. |

The development guide identified existing [worker operations](../background-tasks.md).
No schema migration was required. Finish queued iMessage imports before upgrading
or start fresh with the same source/settings afterward; see
[import provenance](../analysis-communications.md#message-csv-imports).

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
