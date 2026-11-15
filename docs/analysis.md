# Case analysis and extraction

This guide owns analysis behavior, retained results, and processing configuration.
For screen navigation and control placement, see the
[interface specification](interface-design.md#case-tools-processing-and-additional-analysis).
Adding eligible evidence queues supported local checks; viewing results does not
require review. Owners and editors can start or cancel processing and append
reviews; viewers inspect authorized originals and results. Case permissions apply
to workers, search, downloads and playback.

The case highlights inspect the latest 40 eligible published result runs, keep the latest
result per operation and evidence/attachment, and show at most 12 findings.
They revalidate sources and omit archived, unavailable, rejected, and weak OCR
findings. This is a recent highlights view; **Browse all findings** provides the
complete retained collection. Full runs remain accessible through Processing
history. The Vehicle trips report is computed from current observations on opening
the report, including previously imported data.

**Browse all findings** searches complete retained labels, text and measurement
values, with 25 findings per page and filters for method, evidence type, specific
evidence, explicitly related person, and finding-review state. An evidence reader's
**All findings from this evidence** link applies the exact source filter. Archived
sources, weak plate regions, and all review states remain visible with labels.
Whole-result review is displayed separately and does not accept individual findings.

The default selects the newest requested successful equivalent run before applying
search/review filters. Equivalence includes method/version, execution configuration,
meaningful parameters, and ordered exact source versions/checksums; automatic-check
bookkeeping and requester identity do not distinguish equivalent work. A successful
empty run supersedes older findings; failed, unsupported, insufficient, cancelled,
and pending runs do not. **All retained runs** exposes earlier versions. Different
model settings or extraction versions remain distinct. Missing or changed sources
retain an unavailable row without exposing finding content. Historical geocode
decisions remain inspectable, with a warning if current review has changed.

Person filters use only explicitly linked person-location observations and
communication sender/recipient records actually cited by each finding. Other
sources in the same run, matching names, and contact strings do not establish a
person association. Person detail links to this collection and related evidence.

The finding catalogue is a relational projection published in the same transaction
as each immutable result. Its additive backfill reads saved JSON only, commits one
run at a time, and resumes completed work; it neither reruns analysis nor reads
original files. Stop old writers/workers before migrating and restart updated
services afterward. Projections are never rebuilt during application startup.

**Case Tools → Connections** groups exact candidate strings found in retained
analysis: emails, phone candidates, complete URLs, bare domains, explicit
identifier candidates, and non-weak plate-text candidates. Email local-part case,
phone international-prefix status, URL paths, and identifier case are preserved;
domains/URL hosts are lowercased. Plate text is compared as uppercase ASCII letters
and digits. Complete URLs and bare domains are separate kinds. These comparisons
do not establish identity, ownership, association, or independent corroboration.

Groups count distinct original/attachment references, collapsing repeated runs and
extraction versions. Matching checksums warn about possible copies. Counts describe
retained references; opening the group validates the current page's sources, with
unavailable rows retained. A group identifier is shown only after validating its
representative finding's sources. Review states and partial coverage remain visible.
Coverage depends on completed extraction/checks and their limits: for example,
identifier extraction can stop after 2,000 occurrences. An empty Connections view
does not establish that no connection exists. Use the linked extraction and
identifier checks to extend coverage.

Result/provenance and retained geographic reference downloads keep their `.json`
filenames and JSON content. They use `application/octet-stream` with attachment
disposition so browsers, including Linux WebKit, save the files reliably. Downloads
remain private and uncached; image previews remain inline JPEGs.

The [roadmap](evidence-analysis-roadmap.md) covers future work. This guide and the
focused guides below own current capabilities and supported formats.

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

Analysis reports cover call counts/durations/timing, vehicle trips, observation coverage
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

An evidence reader shows up to three highlights per latest published operation,
twelve total. Rejected results and weak OCR are omitted; partial coverage remains
visible. **Processing** retains all outcomes across evidence. Preparation is
separate from execution; batch membership includes extraction follow-ups. Imports
ready for review remain in progress until investigators finish that deliberate
step. Queued counts are not completion counts. The
[background task guide](background-tasks.md#investigator-progress-and-completed-imports)
explains progress and import recovery.

Owners and editors can use **Processing → Run more checks → Rerun all analysis** to
queue the same supported local checks for all active evidence, including every
retained call transcript/recording. Archived evidence and material added after the
request are excluded. Preparation runs in background steps of at most 25 sources;
the page shows preparation progress, unavailable-source counts and queued checks.
Refreshing or resubmitting the same request does not duplicate it. A new click
after refreshing deliberately starts another batch, preserving earlier results
and reviews. AI inference, transcription and external lookups still require their
existing deliberate analysis flows. Empty calls have no retained content to check.
CSV imports still require timezone/vehicle confirmation before atomic publication.
Trips then appear without per-row jobs, geocoding, or review. Conversation imports
retain their dedicated flow. An uncertain save retried with its original submission
recovers the same evidence and checks.

Completed vehicle and iMessage imports offer a deliberate **Run local checks on
imported evidence** action. Independent checks use bounded groups rather than one
batch per imported row; skipped duplicates are excluded. See
[background tasks](background-tasks.md#investigator-progress-and-completed-imports).

Source choices display prerequisites and retained inspection status. A source may
need exact extraction, a transcript, or accepted coordinates. Unknown file support
is **Not yet inspected**, not a successful parser claim. Configuration readiness
uses local validation only; live runtime connectivity and identity are checked
when processing starts. Selections are never silently reduced to compatible rows.

[Shared selections](shared-selections.md) preserve exact references for repeat
analysis and printable briefs. [Question-driven synthesis](synthesis.md) uses
selected text and explicitly chosen findings, with separate quotation types.
[Chronology](chronology.md) keeps observed and acquired times distinct.
[Original previews](original-previews.md) provide private PDF/text/WAV reading.

Use **Additional analysis** for comparisons, geographic lookups, model processing,
or conversation imports. Choose a task, exact retained sources (including
extraction versions), and required context before starting. The source picker
shows at most 500 choices; title search narrows larger cases. Only the chosen
operation's settings are validated and saved.

**Read inferred message sessions**, **Inspect message timing**, and **Summarize
message sessions** select Text messages only. For retained call transcripts, use
**Inspect keywords and topics** or **Suggest claims and commitments**; **Compare
exchanges across channels** still accepts messages, phone calls and FaceTime.
Earlier runs retain their original selections and results.

Model processing is deliberate: **Local** is the default; **Cloudflare AI Gateway**
requires explicit external-processing approval. Local failure never causes cloud
fallback. Geographic services require approval unless the operator designates the
endpoint local. Keyword checks do not implicitly request AI summaries. The Map's
separate [local address lookup](maps.md#local-address-lookup) follows its own flow.

Call and vehicle reports use chronological rows in the user's timezone with source
links. Their filters use the same timezone; recorded UTC hour counts retain their
stated analysis basis. Truncation and missing-time warnings remain visible.
Duplicate reports group file-byte checksums; trip rows retain departure/arrival
links and the full observation list.

## Inspecting run results

Run results retain exact source links, processing details, limitations, and
separate finding/overall reviews:

- Image metadata distinguishes embedded claims from supplied metadata. OCR/plate
  findings show retained image regions/crops, separate plate-likeness and OCR
  scores, and low-score notices. Identifier normalization is in **Value details**.
- Document text retains native page/line anchors. Recording findings pair timed
  intervals with playback of the selected recording/transcript.
- Wording/style comparisons show both samples; model suggestions cite supporting
  passages. Conversation/import findings preserve participants, source labels,
  timing, and reasons for unresolved rows.
- [iMazing CSV imports](analysis-communications.md#imazing-csv-message-imports)
  prepare a separate review before publishing messages and any confirmed new
  people. A completed preview means the review is ready, not that messages were
  imported.
- Location schematics distinguish reported positions, reviewed estimates, and
  lookup candidates. Measurements and retained map references link to sources.

A run page shows up to 50 findings. Derived inputs link to both the retained
extraction and original.

Processing outcome, reviewed-finding count, and overall review are distinct.
**Awaiting review** means no decision is recorded; **Unresolved** is a review
choice. Owners/editors can append individual and overall reviews; geocode reviews
apply per candidate. Viewers can inspect them.

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
starting another run while preserving the earlier queue records. **Start another
run with these inputs** keeps the source selection and uses current processing
settings. The investigator starting the run must review and approve any external
destination. If the settings change after the approval form is displayed, review
the updated destination before submitting again. Local reruns use the same button
without external approval. If a worker crashes with a run still marked running,
confirm it has stopped before cancellation or recovery. A stopped worker may
have sent a request, and cancelled results remain withheld.
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
- [Geographic context and retained reference snapshots](analysis-geographic-context.md)
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

See [local analysis](local-analysis.md) for the supported runtime-backed methods,
pinned model/engine identities, configuration, capacity, qualification, and
failure recovery. Private inference is the default; every run retains its selected
routing/settings and provenance. Queued configuration drift fails recoverably,
without switching endpoints or falling back to external AI.

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

`core.evidence.types` registers the ten evidence types; `core.evidence.queries`
checks same-case relationships. `core.analysis.reports` dispatches reports to
phone/geo implementations; cross-type duplicate reports and neutral helpers stay
in `core`.

The [architecture guide](architecture.md#dependency-boundaries) owns domain
categories, composition, and parser boundaries. `core.analysis.services` owns
authorization, source snapshots, publication, and reviews;
`core.analysis.operations` dispatches concrete callables with explicit dependencies.
The UUID-only task entrypoint keeps durable runs/results/reviews and search text
separate from disposable queue records.

Shared form factories select ordinary domain form subclasses; shared bases own
source selection, submission, processing location/approval, archive and period
fields. Domain forms retain operation-specific validation and serialization.

Source resolution reuses originals and prior results only within one validation
pass. Search similarly validates a shared run once per request. External requests
and publication still perform fresh authorization and source checks, so this
reuse does not preserve access or acceptance after it changes.

Shared processing models belong to `core`; concrete processors belong to domain
apps. Use the [fresh migration baseline](architecture.md#primary-keys-and-the-initial-schema).
Back up PostgreSQL and private storage together. No global analysis admin or public
result-storage endpoint is registered.

## Deleting an imported source

Superusers with current case access can confirm source deletion from an evidence
reader's **More → Delete evidence** or scoped admin. The [evidence deletion workflow](evidence.md#permanent-deletion)
owns the complete inventory, stale-confirmation checks, dependent analysis/import
cascade, publication locking, and post-commit file cleanup. Whole-case deletion
also removes analysis/context. Deletion does not remove unrelated originals,
people, or vehicles.
