# Case analysis and extraction

The case **Analysis** workspace keeps current-field reports separate from retained
processing runs. Owners and editors can start/cancel processing and append reviews;
viewers inspect authorized reports, originals, and results. The case boundary also
applies to workers, search, result downloads, and inline recording playback.

Result/provenance and retained geographic reference downloads keep their `.json`
filenames and JSON content. They use `application/octet-stream` with attachment
disposition so browsers, including Linux WebKit, save the files reliably. Downloads
remain private and uncached; image previews remain inline JPEGs.

The full product catalogue and recommended order remain in
[evidence analysis](evidence-analysis.md) and the [roadmap](evidence-analysis-roadmap.md).
Implementation and supported formats must be judged against this guide and the
focused guides below, rather than treating every catalogue possibility as shipped.

## Reports and search

The Evidence collection searches original Text and Text message bodies, every
supplied call transcript, and completed/partial extraction passages. Matching call
additions do not multiply the parent call. A retained-text match opens its exact
addition; an extraction match opens the retained processing version. Queries keep
case, type, archive, and pagination boundaries. Text resembling HTML is displayed
as text.

Extraction excerpts use one bounded candidate query for the current evidence
page, considering at most ten matching passages per record. Each candidate must
match its retained source, checksum, and call attachment. An unavailable newer
run can fall back to an older valid candidate within that limit.

Analysis reports cover call counts/durations/timing, vehicle observation coverage
and receipt delays, reported speed/heading, and exact file-byte duplicates. Each
report bounds its source selection to 500 records and discloses truncation,
unknown values, archive/time filters, and contributing originals. Call audio uses
its audio-byte checksum; a call/attachment manifest checksum is a different hash
domain. Unknown duration or speed is distinct from zero. Gaps do not prove
inactivity; repeated addresses do not establish visits.

## Retained processing

Keyword/topic and session-summary methods use version 2 to preserve stage warnings
and combined outcomes: retained keywords remain useful if topic inference finds
nothing, while missing session content remains insufficient or partial. Pending
version 1 runs of `text_topics` and `session_summaries` fail with
`processing_version_unavailable`; start another run to use the current method.
Earlier results and reviews remain unchanged.

Adding evidence through the investigator's single-item or bulk upload queues
applicable local checks in the background. These checks cover native document
text extraction, image metadata, printed-English OCR and possible vehicle-plate
search, audio metadata,
identifiers, dates and keywords in available text, and location quality with
string normalization when a recorded address is present. A new call transcript
or recording is processed independently of earlier additions. Text checks after
extraction use that exact retained extraction version. Unsupported formats and
insufficient content appear as analysis outcomes.

Open **Analysis** or **View analysis** on an evidence record to see source-linked
findings. Images surface possible plate readings, OCR, capture time and GPS
claims. Documents and text show extracted passages and identifiers with source
positions. Audio and call findings identify the retained recording or transcript
and show intervals where available. Communication records show reported participants
and event time. Location records show reported position and observation time,
associated people or vehicles, and the retained CSV row when applicable.
Reviewed geographic estimates are labelled alongside the reported evidence.
The tab shows up to three findings
from the latest published result for each analysis type, with a maximum of
twelve. Each highlight cites the open evidence and links to the exact finding,
its sources, and review history. Review decisions and partial coverage appear
alongside the values. Rejected findings and results are omitted from highlights.

**Run history** below the findings and analysis controls keeps all runs and
processing outcomes, including earlier results and work still in progress.
**View run history** jumps to that section. **Refresh status** appears while
analysis for the evidence is queued or running.
Owners and editors can **Analyze evidence** for a record without runs or **Run
local analysis again** once the current checks finish. The Original/Analysis/Details
navigation keeps collection filters, and run history paginates independently.
Original bytes and earlier runs remain retained. Archived evidence can still be analyzed.
Vehicle CSV and conversation imports use their dedicated review and processing
flows without launching automatic checks for each imported row.
If a save cannot be confirmed, retry the same upload submission so the original
and its checks can be recovered together.

Additional model processing is a deliberate choice. Select the operation and
retained source, review its settings, and choose **Local** (the default) or
**Cloudflare AI Gateway**. Cloud processing requires explicit submission approval;
local failure never triggers cloud fallback. Geographic services require external
approval unless the operator explicitly designates the endpoint as local.
Local keyword analysis does not enable an AI summary automatically.
Use **Start analysis** for context-dependent comparisons, geographic lookup or
context, and conversation imports. Choose sources and review assumptions before
queueing. The separate Map workflow also supports local address lookups. See
[Map address lookup](maps.md#local-address-lookup).

Case Analysis shows recent runs with their evidence types, source count and
outcome. Open **Run filters** to filter by evidence type or status. **Back to analysis** from a run
opened in that list returns to the same filters and page. Reports sit alongside
the runs on wide screens and can be reached with **View reports** on smaller
screens. Choose **Start analysis**, select a task, and choose **Show sources**
to jump to the eligible sources. The form describes the result and identifies
originals, additions, extraction versions and reviewed estimates. The selected
operation appears beside **Queue analysis**.
The picker shows at most 500 choices; use its title search to narrow larger cases.
Select an exact extraction version to analyze derived text. Review grouping/time
assumptions before starting an investigation workflow.
Only the selected operation's settings are validated and saved. Operations without
optional assumptions do not show an empty assumptions section. Report forms load
only the people or vehicle choices applicable to that report. **Report filters**
starts collapsed for the default selection, leaving results visible first. It opens
when filters are applied or a submitted selection needs correction. Reports show
records before detailed totals, with **View records** and **View totals** links
for jumping between them. Call and vehicle reports show chronological rows
with UTC dates and source links. The duplicate report groups occurrences by
file-byte checksum. Open **Selection and additional totals** for the remaining
counts and coverage measures.

## Inspecting run results

**Findings** shows output, source links and the latest review state on each page.
Image metadata uses field-and-value tables by source, and embedded tags carry an
unverified-claim label. Read OCR candidates alongside their retained image regions.
Plate readings are grouped by region with their available crops and separate
plate-likeness and OCR scores. Document text carries native page or line anchors.

Recording results show properties and timed intervals by retained recording,
with a player and links to individual intervals. Identifier and keyword tables
include source citations. Wording comparisons show paired quotations, and writing
comparisons show measurements for both samples. Model suggestions identify their
supporting passages. Conversation results show timing and participants alongside
the source messages or calls. iMessage results show raw source labels, mapped
participants and time, and the reason for each unresolved row.

Location analysis shows a schematic of positions linked to findings on the page.
Markers distinguish reported positions, reviewed estimates and lookup candidates.
Rows show the measurements that matter for the operation, such as sampling gaps,
time and distance differences, or period coverage. Geographic context findings
link to their retained map reference. A findings page contains up to 50 retained
findings, grouped within that page.

Open a finding for its individual review and exact source context. A run with one
finding opens it directly. **Back to findings** returns to
the same page and finding. Source links include exact anchors where available.
The run header links to findings, available review controls and run details.
When a run uses a single evidence record, **View evidence analysis** opens its
Analysis tab. A focused plate finding shows its available crop and marked image
region together. Focused recording findings place playback beside the interval.
Derived inputs link to their retained extraction and to the original. Findings
with a low OCR engine score show a notice beside the reading.

The processing outcome, count of findings with recorded reviews, and overall
result review are separate. **Awaiting review** means there is no recorded decision
at that scope. **Unresolved** is a decision a reviewer can record. Owners and editors
can review the selected finding. Geocode reviews apply to individual candidates.
Viewers can inspect output, sources and reviews. Saving returns to the same finding.
If validation fails, the entered decision and reason stay available for correction.

Open **Limitations** for the full result warnings and **Review history** for recorded
finding and overall decisions. **Run details** groups processing totals, selected
inputs, method and requester attribution. Where present, the coordinate map and
retained map references can be opened on request. The links and disclosures work
with a keyboard and without JavaScript.

**Download results** saves private JSON containing the result, selected sources,
and processing provenance. If an exact retained input is unavailable or changed,
the run withholds the result and its download.

The PostgreSQL worker described in [background tasks](background-tasks.md) executes
queued work. Queued, running, complete, partial, no findings, insufficient evidence,
unsupported, failed, and cancelled outcomes remain distinct. Refresh the run page
for progress. A result retains its source selection/checksums, parameters, method
version, anchors, and separate review history. Another run retains earlier output.

Cancellation prevents subsequent publication; a previously sent external request
cannot be recalled. A queued job runs only for a currently active, verified owner
or editor, with source relationships rechecked before work, external submission,
and atomic publication. Access removal blocks later inspection as well as work.

A duplicate delivery cannot claim a running/completed run again. Retrying a form
with its original submission ID returns the same run. Recover a failed run by
starting another run; do not reset queue rows. If a worker crashes with a run still
marked running, confirm it has stopped before cancellation/recovery. A stopped
worker may have dispatched an external request; cancelled results remain withheld.
The same-database queue insert and new run commit or roll back together.

Originals and supplied metadata remain separate from extracted observations, model
suggestions, and investigator conclusions. Result acceptance is a review decision,
not verification of identity or truth. Every finding can retain its own review.
Per-candidate geocode acceptance is required before using a selected estimate in
another location analysis. Downstream runs retain the accepted review ID; later
review changes block new processing while preserving historical results and a
visible warning.

## Supported operation guides

- [Documents, images, recordings, OCR, and transcription](analysis-extraction.md)
  describes the bounded format matrix, parser isolation, source anchors, partial
  coverage, and synthetic evaluation.
- [Text, writing styles, conversations, and imports](analysis-communications.md)
  describes exact spans, sample adequacy, native versus inferred sessions, explicit
  account mapping, and the supported iMessage HTML profile.
- [Locations, journeys, visits, and geographic lookup](analysis-locations.md)
  describes grouping assumptions, gaps, observed dwell bounds, proximity,
  co-location, geographic estimates, and source-specific limits.
- [Automatic geographic context and retained reference snapshots](analysis-geographic-context.md)
  describes road, venue, building and neighbourhood candidates, historical map
  snapshots, private reference downloads and operator configuration.

Image OCR runs locally with Tesseract and English language data. It requires no
AI credentials or external approval and never falls back to a remote service.
Handwriting and mixed-text profiles are unsupported by the local replacement;
older results remain readable. Map context uses a separate configured
reference service and does not require AI credentials. Address-only miTrail
observations first need an explicitly reviewed geocode. Local spatial grouping
continues to work without an external map service.

## Local inference configuration

Recording transcription and the four model-based text methods use the configured
private runtime by default. Models and native engines have pinned identities;
each new run retains its routing/settings snapshot and results preserve model
provenance. Existing runs retain their historical routing and approval. A queued
configuration/model mismatch fails recoverably rather than switching endpoints.
Text tokenization checks the full rendered prompt plus output allowance; content
is never silently truncated. See [local analysis](local-analysis.md) for optional
Compose runtimes, configuration, GPU qualification and failure recovery.

## External AI configuration

Explicitly selected external ML/LLM inference uses the centralized backend
Cloudflare AI Gateway client.
No credentials or direct-provider inference run in the browser. The supported
transport is the provider-native Workers AI endpoint at
`gateway.ai.cloudflare.com/v1/ACCOUNT/GATEWAY/workers-ai/MODEL`. Authenticated Gateway
and upstream Workers AI credentials are configured independently. See the official
[Gateway authentication](https://developers.cloudflare.com/ai-gateway/configuration/authentication/)
and [Workers AI integration](https://developers.cloudflare.com/ai-gateway/usage/providers/workersai/)
references; the provider-native endpoint continues to be supported.

| Environment variable | Purpose/default |
| --- | --- |
| `PANDECT_AI_ACCOUNT_ID` | Cloudflare account ID; empty disables inference. |
| `PANDECT_AI_GATEWAY_ID` | Operator gateway ID. |
| `PANDECT_AI_GATEWAY_TOKEN` | Authenticated Gateway token, sent as `cf-aig-authorization`. |
| `PANDECT_AI_WORKERS_TOKEN` | Workers AI token, sent as `Authorization`; separate upstream credential. |
| `PANDECT_AI_TEXT_MODEL` | `@cf/meta/llama-3.3-70b-instruct-fp8-fast` |
| `PANDECT_AI_TRANSCRIPTION_MODEL` | `@cf/openai/whisper-large-v3-turbo` |
| `PANDECT_AI_TIMEOUT` | Request timeout in seconds, default 60, maximum 120. |
| `PANDECT_AI_DAILY_CASE_REQUESTS` | Daily per-case request count ceiling, default 100. |
| `PANDECT_AI_MAX_REQUEST_BYTES` | Submitted request ceiling, default 8 MiB. |
| `PANDECT_AI_MAX_RESPONSE_BYTES` | Response ceiling, default 2 MiB. |

Workers and web must receive matching configuration. Operators configure logging
and retention on their Gateway/provider deliberately. The client sends skip-cache
and no-log headers, but these do not replace an operator policy or guarantee all
upstream retention behavior. Explicit external-processing approval is recorded
with the run. Payloads, credentials, and provider exception bodies do not enter
routine application telemetry.

Every HTTP attempt has case/run/model attribution and byte counts. Available token
counts are retained; absent token usage stays unknown. Limits count requests,
including failed attempts, rather than inventing exact cost. Authentication,
quota, throttling, timeout, invalid-output, and missing-configuration failures are
recoverable. Only transient 429/502/503/504 failures have one bounded transport
retry, with current authorization rechecked. Other failures require a deliberate
new run. Ordinary reports and deterministic analyses work without AI credentials.

Routine tests use synthetic material and mocked inference/geocoding. Local checks
do not establish live provider availability or production-quality performance for
an operator-selected model. Evaluate a changed model/language/input matrix before
advertising broader supported conditions.

## Data and code boundaries

`core.evidence.types` is the shared ten-type registry; `core.evidence.queries` enforces
same-case relationships. `core.analysis.reports` validates and dispatches current-field
reports; call calculations live in `phone.analysis.deterministic.reports` and vehicle
calculations in `geo.analysis.deterministic.reports`. Cross-type duplicate reports
remain in `core.analysis.deterministic.reports` with neutral report helpers in
`core.analysis.reporting`.

Concrete operations live in each domain's `analysis/` package:

| Code category | Implementations |
| --- | --- |
| `deterministic` | Document text extraction; text identifiers, style, repeated wording and keywords; phone conversation grouping, timing, imports and call reports; geographic calculations and vehicle reports |
| `metadata` | Image and audio metadata inspection |
| `ai` | Local image OCR, audio transcription, text suggestions and summaries, and semantic conversation analysis |
| `external` | Geographic coordinate/address and map-context service lookups |

Only implemented categories have modules. AI includes model processing on local,
private, or approved gateway routes; External includes service lookups on private
or public routes. Categories do not determine consent or network destination.
They organize code only: analysis choices, forms, and automatic checks are unchanged.
`text.analysis.workflows` and `phone.analysis.workflows` compose the existing optional
AI branches with deterministic results. Communication import parsing and publication
live in `phone.analysis.deterministic.imports`; geographic adapters live in
`geo.analysis.external.geocoding` and `geo.analysis.external.geographic_context`.
`core.analysis.services` owns authorization, input snapshots, publication, and reviews.
`core.analysis.operations` lists concrete callables; the worker passes authorization
and inference dependencies explicitly for the operations that use them.
`core.analysis.tasks` is a thin UUID-only queue entrypoint. Durable runs/results/reviews
and extraction search passages survive operational queue housekeeping.

The `analysis_form` factory in `core.analysis.forms` selects ordinary Django form
subclasses from the domain `analysis_forms` modules. Image, geographic, communication,
and text assumptions retain their own fields, validation, and parameter serialization.
`core.analysis.form_base` owns shared source selection, submissions, processing location,
and external-processing approval.
The `report_form` factory similarly selects phone/geo report filters while
`core.analysis.report_forms` owns common archive and period fields.

`core.parsers` owns verified file reads and the bounded subprocess transport.
Its fixed operation allowlist dispatches to the domain `parsers` modules in
`document`, `image`, `audio`, and `geo`, and to `image.analysis.ai_parser` for local
OCR. The image parser retains shared decoding, metadata, and preview preparation;
the AI parser owns Tesseract execution and recognized-region interpretation.
The pure parser contract and these domain modules load without Django initialization.
The child runs with isolated Python,
a restricted environment, and resource limits established before domain parsing;
it imports application code only from the trusted root derived from the runner.
The shared runner retains timeout, output-size, and process-group cleanup checks.

Source resolution reuses originals and prior results only within one validation
pass. Search similarly validates a shared run once per request. External requests
and publication still perform fresh authorization and source checks, so this
reuse does not preserve access or acceptance after it changes.

The shared processing models belong to `core`; concrete processors belong to
the domain apps. Use the [fresh migration baseline](architecture.md#primary-keys-and-the-initial-schema)
for this pre-release refactor. Back up PostgreSQL and private storage together. Whole-case
deletion removes its analysis/context alongside evidence. No global analysis admin
or public result storage endpoint is registered.


## Deleting an imported source

A superuser with current case access can delete a retained source document from
Evidence Details. The confirmation lists its imported messages/vehicle-location
observations, source context and review rows, and all dependent analysis, reviews,
extracted text, and generated files. Multi-source runs and downstream results are
removed as a whole; unrelated originals, people, and vehicles remain. The same
exact inventory is used in Django admin. It is paginated with a complete private
JSON download, and a changed inventory requires another confirmation.

Deletion locks the case against new import and analysis publication. Queued jobs
for removed runs/imports become unavailable; running jobs cannot recreate deleted
sources or publish new derivatives after deletion. Row deletion is atomic and
stored-file cleanup waits for commit. Storage failures retain the existing
private-key logging and manual retry policy in
[evidence](evidence.md#private-storage-and-deletion).
