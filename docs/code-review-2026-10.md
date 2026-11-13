# Code review catalogue — 30 October 2026

Historical review of the dated snapshot. Paths, counts, and validation results
below describe that review, not the current checkout. Use [validation](validation.md)
for current commands and [architecture](architecture.md) for current ownership.

This review covered the first-party application, native inference runtime,
templates, JavaScript/CSS, migrations, tests and operational configuration,
including the feature work already present in the working tree. Vendored libraries
are inspected at their integration boundary, not rewritten. Findings below are
concrete maintenance or correctness issues; repeated syntax alone is not a reason
to introduce an abstraction. Review does not prove the absence of other defects.

The starting working tree was preserved separately so the final review can isolate
these fixes from earlier work. No runtime data, dependency versions or schema
changes are intended.

## Findings

| ID | Area and finding | Smallest useful correction | Status |
| --- | --- | --- | --- |
| A1 | `analysis/text.py`: topic keywords inherit a model-only outcome, so useful keyword results can be labelled “No findings” or unsupported. | Derive the combined outcome from both stages and retain model limitations. | Fixed |
| A2 | `analysis/conversations.py`: session summary aggregation drops child warnings/outcomes. | Preserve uncertainty and distinguish empty output from insufficient input. | Fixed |
| A3 | Geocoder/context/Gateway HTTP failures leave response streams open, including retry paths. | Close each HTTP error response deterministically; retain bounded retries and safe errors. | Fixed |
| A4 | Analysis assumption controls have drifted from the location algorithms: some useful thresholds are hidden while unused settings are shown. | Align each operation's fields with the settings it consumes and exercise the forms in browsers. | Fixed |
| A5 | Analysis modules redefine evidence-kind families already maintained in `evidence/types.py`. | Import the canonical kind tuples. | Fixed |
| A6 | Analysis publication removes newly stored artifacts after any transaction error, including an uncertain commit that may have retained their result rows. | Clean artifacts only after a failure in the publication body; retain potentially committed artifacts for reconciliation. | Fixed |
| I1 | Local and Gateway adapters duplicate redirect blocking and JSON/prompt rules; cloud parsing accepts nonfinite values. | Share small protocol helpers and reject malformed/nonfinite model input/output consistently, keeping transports separate. | Fixed |
| I2 | `LocalClient` stores an unused run; qualification fabricates a run just to satisfy it. | Remove the unused dependency and dummy object. | Fixed |
| I3 | `execution_changed` has no actionable recovery message. | Explain configuration drift and how to start a new run safely. | Fixed |
| E1 | People, vehicle and evidence admins repeat the same case access policy. | Extract one focused case-scoped admin base, retaining specialized evidence validation and deletion rules. | Fixed |
| E2 | Vehicle detail reads recent locations without the established related-case evidence filter. | Reuse `same_case_evidence` and test inconsistent imported relationships. | Fixed |
| E3 | Evidence navigation builds the same query twice, with inconsistent search bounds. | Reuse the existing query builder for pagination and detail navigation. | Fixed |
| E4 | A file written before a failed model INSERT can remain orphaned; CSV cleanup also deletes files after uncertain commit errors. | Roll back failed saves before cleaning new keys; preserve files on uncertain commits, primary exceptions and existing originals. Later caller rollback remains an explicit limit. | Fixed |
| E5 | Product and admin evidence forms duplicate upload/text/description validation. | Share a small base form with those exact rules. | Fixed |
| E6 | Original and call-attachment downloads repeat file-open and unavailable-response handling. | Share a private download helper within the existing view module. | Fixed |
| C1 | Case list builds a dictionary of every membership and separately fetches visible cases; concurrent revocation can leave a missing role. | Annotate the current role in the paginated query using the existing membership relation. | Fixed |
| C2 | Case overview/deletion count each evidence kind twice for active/archived totals. | Use a filtered aggregate per kind while preserving distinct reader and deletion scopes. | Fixed |
| C3 | Error and evidence views duplicate the existing JSON-response preference helper; people/vehicle views supply unused navigation context. | Reuse `config.access.wants_json` and remove unused context. | Fixed |
| C4 | `request.POST or None` treats an empty submission as an unbound form and suppresses required-field errors. | Bind forms based on request method in case/collaboration and attachment workflows. | Fixed |
| V1 | `make browser-check` and CI omit the existing analysis, context and map browser suites. | Include all maintained browser suites in the common validation target. | Fixed |
| V2 | The isolated Compose smoke script manually unsets an outdated list of Pandect settings, leaving newer private-runtime credentials/endpoints and limits inherited. | Clear the entire `PANDECT_` environment namespace before applying synthetic configuration. | Fixed |
| F1 | Removing a single-page upload selection can prevent reselecting the same file because the native picker retains its value. | Clear the picker after retaining selected `File` objects for both queue modes. | Fixed |
| F2 | Asynchronous map-marker replacement destroys keyboard focus inside an open source popup. | Restore the focused source/review link after rebuilding its popup, with a safe fallback if the point disappears. | Fixed |

All 23 catalogued findings are fixed. Validation and final review results are
recorded below; this catalogue is bounded to the reviewed working-tree snapshot.

## Reviewed coverage

- Accounts, case membership/invitations, configuration, privacy middleware and
  background diagnostics, including their routes, migrations and tests.
- People, vehicles, all retained evidence types, imports, storage, forms,
  administration, search, previews, private downloads and tests.
- Analysis operations, algorithms, parsers, sources, models, publication, jobs,
  automatic planning, views/forms, navigation, reports, artifacts and tests.
- Local/Gateway clients, execution identity, qualification command and standalone
  native runtime, including failure-path tests.
- All first-party templates, CSS and JavaScript, browser harnesses, build/Compose
  files, dependency/lock configuration, CI workflows and smoke script. Leaflet was
  reviewed at the local asset/CSP/DOM integration boundary.

## Deliberate nonchanges

- Keep explicit people/vehicle/evidence workflows rather than a configurable CRUD
  framework. Their integrity, deletion and edit-conflict semantics differ.
- Keep immutable evidence bases, source validation, case locks and permission
  rechecks. These are required safeguards, not redundant validation.
- Keep native inference process isolation, pinned identity and separate local/cloud
  adapters. Sharing a redirect policy does not justify a generic HTTP framework.
- Keep historical migrations and vendored assets intact; ordinary source cleanup
  must not rewrite schema history or third-party code.

## Validation

- Baseline: 708 tests passed; Ruff, lockfile, Django system/database, migration
  drift and Compose configuration checks passed.
- Final `make check`: all 729 tests passed, including native-runtime and real
  database-commit failure coverage. Ruff, lockfile, Django system/database,
  migration drift and Compose configuration checks passed. No new migrations.
- Disposable Compose startup, persistent storage, worker restart/extraction and
  production static-serving checks passed. Synthetic invalid inherited runtime,
  tile and upload settings verified the smoke test's environment isolation.
- Locked dependency audit: no known vulnerabilities reported.
- All 27 browser journeys passed across Chromium/WebKit: 26 passed in the full
  run; the map-focus journey passed on a focused rerun after correcting its new
  mocked-response fixture. Checks include no-JavaScript, keyboard controls,
  desktop/mobile/reflow, appearance preferences, access revocation and originals.
- JavaScript syntax, shell syntax and diff whitespace checks passed. Desktop and
  mobile captures of the changed forms and map were visually inspected. Captures
  remain in ignored `data/validation/analysis-browser` and `data/validation/maps`.
- Local test logs and the task-only diff are in
  `/private/tmp/pandect-review-20261030/`. The diff isolates these changes from
  unrelated preexisting work.
- Required `simplicity_reviewer` review completed against the task-only diff,
  including new files: no actionable overengineering, material findings or
  optional simplifications.

No live deployment or real model/provider qualification is claimed. Screen-reader
behavior was not tested in that review. Storage still requires reconciliation after
process crashes, later caller-owned rollback or uncertain commit outcomes; files
are retained whenever deleting them could break a committed record.

## Upgrade implications

No dependency or schema changes are needed. After upgrading both web and worker,
start new runs for queued version-1 `text_topics`/`session_summaries` methods or
inference adapter snapshots: they fail recoverably rather than silently changing
interpretation. Existing results and reviews remain retained. See
[analysis](analysis.md#retained-processing) and
[local inference](local-analysis.md) for the version and recovery details.
