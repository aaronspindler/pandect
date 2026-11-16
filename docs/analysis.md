# Case analysis and extraction

This guide owns analysis behavior, retained results and configuration; the
[interface guide](interface-design.md#case-tools-processing-and-additional-analysis)
owns navigation. Eligible evidence queues local checks automatically. Owners/editors
start/cancel work and append reviews; viewers inspect originals/results without
reviewing them. Case permissions apply to workers, search, downloads and playback.

Case highlights inspect 40 recent eligible published runs, keep the latest result
per operation/source or attachment, and show 12 findings at most. Source validation
excludes archived/unavailable, rejected and weak OCR findings. **Browse all
findings** exposes the complete collection; Processing retains full run history.
Vehicle trips share saved complete histories with Map/vehicle pages, including
existing imports after preparation.

**Browse all findings** searches retained labels, text and measurements, with 25
findings/page and method/type/evidence/explicit-person/finding-review filters.
**All findings from this evidence** applies the exact-source filter. Archived
sources, weak plate regions and every review state remain labelled. Whole-result
review is separate and accepts no individual finding.

By default, the newest requested successful equivalent run supersedes earlier
findings before search/review filters. Equivalence includes method/version,
execution settings, meaningful parameters and ordered exact source versions/hashes,
excluding automatic-check bookkeeping/requester identity. Successful empty runs
supersede; failed, unsupported, insufficient, cancelled or pending runs do not.
**All retained runs** exposes history. Different settings/extraction versions stay
distinct. Changed/missing sources show unavailable rows without finding content.
Historical geocode decisions remain inspectable with changed-review warnings.

Person filters use explicitly linked person locations and cited communication
senders/recipients. Other run sources, matching names and contact strings establish
no association. Person detail links to findings and related evidence.

The relational catalogue publishes with immutable results. Its additive backfill
reads saved JSON, commits/resumes per run, and neither reruns analysis nor reads
originals. Stop old writers/workers before migration, then restart updated services.
Startup never rebuilds projections.

**Case Tools → Connections** groups exact retained candidate strings: emails,
phones, complete URLs, bare domains, explicit identifiers and non-weak plate text.
It preserves email local-part/identifier case, phone international-prefix status
and URL paths, lowercases domains/hosts, and compares plates as uppercase ASCII
letters/digits. URLs/domains remain separate kinds. Matches establish no identity,
ownership, association or independent corroboration.

Counts collapse repeated runs/extractions into distinct original/attachment
references; matching hashes warn of copies. Opening groups validates current-page
sources, retaining unavailable rows. Identifiers appear only after representative
source validation. Review/partial coverage stays visible; extraction may stop at
2,000 identifier occurrences. An empty view does not establish that no connection exists. Linked extraction/
identifier checks can extend coverage.

Private uncached result/provenance/reference JSON downloads retain `.json` names
and JSON bytes, using `application/octet-stream` plus attachment disposition for
reliable browser saves, including Linux WebKit. Image previews remain inline JPEG.
The [roadmap](evidence-analysis-roadmap.md) separates future work from current guides.

## Reports and search

Evidence search covers original Text/messages, every supplied call transcript and
completed/partial extraction passages. Call additions never multiply parent rows;
matches open the exact addition/extraction version. Case/type/archive/pagination
boundaries apply; HTML-like content displays as text.

One bounded excerpt query considers ten matching passages per current-page
record, validating source, checksum and attachment. Invalid newer runs can fall
back to older candidates within that limit.

Reports cover call counts/durations/timing, trips, observation coverage/receipt
delays, speed/heading and exact byte duplicates. Trips process full histories in
background chunks and paginate complete trips. Other reports stream all matching
case/archive/participant/vehicle/time rows in fetches of 500 or fewer. Source lists
show 25/page; totals, exact medians, duplicate groups and adjacent comparisons
remain complete. Participant pairs paginate separately. Duplicate occurrences
retain full group counts across pages; CSV matches link to full paginated selections.
Unknowns, time exclusions and originals remain visible.

These read-only current-field reports are calculated per request, separately from
saved trips. Call audio uses audio-byte hashes, distinct from manifest hashes.
Unknown duration/speed differs from zero. Gaps do not establish inactivity; repeated
addresses do not establish visits.

## Retained processing

`text_topics` and `session_summaries` version 2 retain stage warnings/combined
outcomes: keywords remain useful without inferred topics; missing session content
stays insufficient/partial. Topic response/quotation validation failures and
inference service failures retain available keyword findings as a partial result,
with a safe explanation and rerun guidance. No rejected topic suggestions are
published; source/access changes and runtime/model identity drift still stop the
run. Pending version 1 fails with
`processing_version_unavailable`; start a new run. Earlier results/reviews stay intact.

Single/bulk investigator uploads queue applicable native text extraction, image
metadata, printed-English OCR, plate search, audio metadata, available-text
identifiers/dates/keywords and location quality/address normalization. New call
transcripts/recordings process independently. Extraction follow-ups bind that exact
version. Unsupported/insufficient inputs remain visible outcomes.

Evidence readers show three highlights per latest published operation, twelve
total, excluding rejected/weak OCR findings while disclosing partial coverage.
Processing retains every outcome. Batch preparation differs from execution and
includes extraction follow-ups; queued counts are not completion. Imports stay
in progress until deliberate review finishes. See [progress/recovery](background-tasks.md#investigator-progress-and-completed-imports).

Owners/editors use **Processing → Run more checks → Rerun all analysis** for active
evidence and all retained call additions at request time. Background steps prepare
25 sources at most, reporting progress, unavailable counts and queued checks.
Resubmitting recovers the request; a fresh click after refresh deliberately starts
a new batch, preserving results/reviews. Empty calls supply no content. This runs
basic local checks; **Reprocess evidence** runs the broader suite. CSV publication
still requires timezone/vehicle confirmation and queues saved trips. Summaries
need no geocoding/review; plotted estimates require review. Conversation imports
keep their flow. Retrying uncertain saves with the original submission recovers
the same evidence/checks.

**Processing → Reprocess evidence** lets owners/editors select types and **Local**,
**External / AI** or **Both**. Active types start checked. Only active case-consistent
sources retained at request time are included. Originals/history stay intact;
resubmissions recover the batch.

Local combines deterministic checks/private inference. External uses Gateway AI
and configured external geography, with local extraction prerequisites. Both runs
deterministic work once and inference per backend. Geography keeps its configured
classification; Both creates no second service. External/Both require transfer
approval. Bound execution settings are rechecked during preparation/follow-ups;
drift stops work, never redirects approval. Missing services fail recoverably
without cloud fallback.

Methods cover inspection/extraction, OCR/plates, transcription, identifiers,
writing/repeated wording, topics/claims, sessions/timing/channel comparison,
location quality/movement and geographic lookup. Derived-text follow-ups bind
exact results. Grouped comparisons disclose selection limits, without claiming
exhaustive cross-group comparisons. Unsupported formats/failures remain visible.
Questions, two-sample writing comparison, area searches, claimed-event comparison
and import mapping require **Choose analysis**; geographic candidates need review.

Completed vehicle/HTML iMessage imports offer deliberate **Run local checks on
imported evidence**, using bounded groups and excluding duplicates; see
[background tasks](background-tasks.md#investigator-progress-and-completed-imports).
Confirmed CSV messages automatically queue bounded message/writing/local-model
analysis for newly published messages. **Follow message analysis** opens the batch;
model failures preserve imports and remain recoverable. See
[CSV analysis](analysis-communications.md#message-csv-imports).

Source choices expose prerequisites/retained inspection status. Some require exact
extractions, transcripts or accepted coordinates. **Not yet inspected** makes no
format-support claim. Readiness checks local configuration; execution checks live
connectivity/identity. Incompatible selections are never silently reduced.
[Shared selections](shared-selections.md) retain repeatable references/briefs;
[synthesis](synthesis.md) distinguishes text/finding citations;
[chronology](chronology.md) separates observed/acquired times;
[previews](original-previews.md) provide private PDF/text/WAV reading.

**Additional analysis** selects task, exact retained sources and context for
comparisons, lookup, model work or conversation imports. The picker shows 25/page,
including retained extraction versions/call additions. Cursor navigation avoids
full-case counts/scans; paging/search preserves selections and permits removal
from other pages. Execution allows 500 sources within parser/inference limits;
only selected-operation settings are validated/saved.

**Read inferred message sessions**, **Inspect message timing** and **Summarize
message sessions** accept Text messages only. Call transcripts use **Inspect
keywords and topics** or **Suggest claims and commitments**. **Compare exchanges
across channels** also accepts calls/FaceTime. Earlier runs keep their selections.

Outside automatic CSV analysis, model work is deliberate. **Local** defaults;
**Cloudflare AI Gateway** requires explicit approval, without cloud fallback.
Geography requires approval unless designated local; keywords never implicitly
request summaries. [Map address lookup](maps.md#local-address-lookup) has its own flow.

Call/vehicle report rows and filters use user timezone, with explicit UTC-hour
analysis and missing-time warnings. Duplicate reports group byte hashes; trips
retain first/last observed links and complete paginated source inspection.

## Inspecting run results

Structured lists/mappings paginate independently at 200 values, retaining finding,
report and collection filters; complete downloads remain available. Parser,
inference and result limits still apply.

Results preserve sources, processing details, limitations and separate reviews:

- Image metadata distinguishes embedded/supplied claims. OCR/plates retain
  regions/crops, separate plate/OCR scores and low-score notices. Identifier
  normalization appears in **Value details**.
- Document text retains native page/line anchors; recording intervals link to
  selected recording/transcript playback.
- Writing/style comparisons show both samples; model claims cite passages.
  Conversations/imports retain participants, labels, times and unresolved reasons.
- [CSV imports](analysis-communications.md#message-csv-imports) publish only after
  separate message/new-person review. Completed preview means ready to review.
- Location schematics distinguish reported positions, reviewed estimates and
  candidates; measurements/map references link to sources.

Runs show 50 findings at most; derived inputs link extraction and original.
Processing outcome, finding-review count and whole-result review remain distinct.
**Awaiting review** means no decision; **Unresolved** is a choice. Owners/editors
append reviews, per candidate for geocodes; viewers inspect.

**Download results** includes private result/source/provenance JSON. Missing/changed
exact inputs withhold results/downloads. [Workers](background-tasks.md) execute
queued runs, distinguishing queued/running/complete/partial/no-findings/insufficient/
unsupported/failed/cancelled. Progress and completed results update in place with
JavaScript; **Refresh status** remains available without it. Runs retain checksums,
parameters, method, anchors and review history; reruns preserve earlier output.

Cancellation blocks publication but recalls no sent request. Workers require
currently active verified owners/editors and recheck sources before work, transfer
and atomic publication. Revoked access blocks work and inspection. Duplicate
delivery cannot reclaim running/completed runs; original submission IDs recover
the same run. Failed work needs a new run, preserving queue history.

**Start another run with these inputs** retains selection but uses current settings.
Its requester reviews/approves external destinations; changed settings require
fresh review. Local reruns need no external approval. Confirm a crashed worker
stopped before cancellation/recovery; it may already have sent requests. Cancelled
output remains withheld. New runs/queue inserts commit or roll back together.

Originals/supplied metadata, extracted observations, suggestions and investigator
conclusions stay distinct. Acceptance verifies no identity/truth. Every finding
can retain review. Spatial inputs require candidate-specific geocode acceptance;
downstream runs retain review IDs. Later changes block new processing, preserving
historical results with warnings.

## Supported operation guides

- [Extraction](analysis-extraction.md): formats, parser isolation, anchors, partial
  coverage and synthetic evaluation for documents/images/audio/OCR/transcription.
- [Communications](analysis-communications.md): text spans, sample adequacy,
  native/inferred sessions, mapping and supported iMessage formats.
- [Locations](analysis-locations.md): gaps, grouping, dwell, proximity, co-location,
  geographic estimates and source limits.
- [Geographic context](analysis-geographic-context.md): feature candidates,
  historical snapshots, private downloads and configuration.

Tesseract English OCR is local, credential-free and never falls back remotely.
Handwriting/mixed profiles are unsupported; old results stay readable. Context
uses a separate reference service without AI credentials. Address-only miTrail
spatial analysis needs reviewed geocodes; local grouping needs no external service.

## Local inference configuration

[Local analysis](local-analysis.md) owns methods, pinned identities, settings,
capacity, qualification and recovery. Private inference defaults; runs retain
routing/provenance. Queued drift fails recoverably without endpoint switching or
external fallback.

## External AI configuration

The centralized backend uses provider-native Workers AI through Cloudflare Gateway:
`gateway.ai.cloudflare.com/v1/ACCOUNT/GATEWAY/workers-ai/MODEL`. Browsers receive no
credentials or direct-provider inference. Gateway/upstream authentication are
separate; see [Gateway authentication](https://developers.cloudflare.com/ai-gateway/configuration/authentication/)
and [Workers AI integration](https://developers.cloudflare.com/ai-gateway/usage/providers/workersai/).

| Environment variable | Purpose/default |
| --- | --- |
| `PANDECT_AI_ACCOUNT_ID` | Cloudflare account ID; empty disables inference. |
| `PANDECT_AI_GATEWAY_ID` | Operator gateway ID. |
| `PANDECT_AI_GATEWAY_TOKEN` | Authenticated Gateway token, sent as `cf-aig-authorization`. |
| `PANDECT_AI_WORKERS_TOKEN` | Workers AI token, sent as `Authorization`; separate upstream credential. |
| `PANDECT_AI_TEXT_MODEL` | `@cf/meta/llama-3.3-70b-instruct-fp8-fast` |
| `PANDECT_AI_TRANSCRIPTION_MODEL` | `@cf/openai/whisper-large-v3-turbo` |

The backend uses a 60-second request deadline, a daily ceiling of 100 requests per
case, an 8 MiB request limit and a 2 MiB response limit. These bounded application
defaults are defined in `config/settings.py`; they are not environment variables.

Match web/worker configuration. Deliberately configure provider/Gateway logging
and retention; skip-cache/no-log headers neither replace that policy nor guarantee
upstream retention behavior. Runs retain explicit approval; payloads, secrets and
provider exception bodies stay out of routine telemetry.

Each HTTP attempt records case/run/model and bytes; missing token usage stays
unknown. Daily request limits include failures and imply no exact cost.
Authentication, quota, throttling, timeout, invalid output and missing settings
fail recoverably. Transient 429/502/503/504 gets one transport retry with renewed
authorization; others need deliberate reruns. Reports/deterministic checks need
no AI credentials. Mocked inference/geocoding establishes no live availability
or model quality; evaluate changed models/languages/inputs before broader claims.

## Data and code boundaries

`core.evidence.types` registers ten types; `core.evidence.queries` checks same-case
references. `core.analysis.reports` dispatches phone/geo reports; duplicates and
neutral helpers stay in core. [Architecture](architecture.md#dependency-boundaries)
owns composition/parser boundaries. `core.analysis.services` handles access,
snapshots, publication/reviews; `operations` dispatches explicit callables. UUID-only
tasks separate durable runs/results/reviews/search from disposable queue records.

Shared factories select domain forms; bases own selection/submission, processing
location/approval, archive and periods. Domain forms validate/serialize operations.
Source resolution reuses data within one validation pass, and search validates
shared runs once/request. Requests/publication recheck access/sources afresh;
reuse cannot preserve revoked access or changed acceptance.

Core owns shared models; domain apps own processors. Follow the
[fresh baseline](architecture.md#primary-keys-and-the-initial-schema) and coordinate
PostgreSQL/private-storage backups. No global analysis admin/public storage exists.

## Deleting an imported source

Superusers with current case access confirm deletion through **More → Delete
evidence** or scoped admin. [Evidence deletion](evidence.md#permanent-deletion)
owns inventories, stale confirmation, dependency cascades, publication locking
and post-commit cleanup. Whole-case deletion removes analysis/context; unrelated
originals, people and vehicles remain outside source deletion.
