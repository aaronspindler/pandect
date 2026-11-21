# Case analysis and extraction

This guide owns analysis behavior, retained results and configuration; the
[interface guide](interface-design.md#case-tools-processing-and-additional-analysis)
owns navigation. Newly added evidence queues applicable analysis automatically. Owners/editors
start and cancel work. Viewers inspect originals and results. Case permissions apply to workers, search, downloads and playback.

Case highlights inspect 40 recent eligible published runs, keep the latest result
per operation/source or attachment, and show 12 findings at most. Source validation
excludes archived or unavailable sources, weak OCR, and timing-only
findings. **Browse all findings** provides access to saved findings; Processing
retains the full results and run history.
Vehicle trips share saved complete histories with Map/vehicle pages, including
existing imports after preparation.

**Browse all findings** searches retained labels, text and measurements, with 25
findings per page and filters for method, evidence type, specific evidence, and person.
The evidence reader applies the exact-source filter directly. Archived
sources and weak plate regions remain labelled.

Message timing reports, writing profile measurements and comparison intervals,
keyword and topic reports, and AI session summaries are available under
**Processing** and in downloads. The catalogue keeps inferred session groups for
conversation navigation. Audio findings include candidate speech with a model
uncertainty flag and a specific playback interval. Complete and partial
transcripts remain available as searchable extracted text.

New claims and semantic links must use a specific supported suggestion type and
cite substantive passages. Factual claims and reported events must identify a
specific event or action and quote its target. General personal evaluations and
unbounded accounts of past experiences stay in Processing. Explicit corrections
publish through claims analysis. Exchange comparisons retain them as context.

Reference and shared-event links require both independent sources to name the
same quoted target. Pronoun-only references, broad shared topics, and links whose
wording still hedges the reference stay in Processing even if a model labels them
resolved. The target uses at least two distinct content words from the English
baseline, or an identifier containing letters and digits. Targets and scope stay
in the saved measurements. Missing or unusable targets keep a suggestion in
Processing. A short yes or no can support a link when a substantive question is also
cited. Exact repeats of the same statement, status and source spans within a run
stay in its results. These rules qualify candidates for review. Exact quotes
establish source wording, and interpretation still requires reviewing those passages.

The same reporting rules apply to known older result shapes. Existing saved
items keep their indexes, links and downloads. Case-specific exclusions from the
findings list preserve those items and their source links.

Evidence findings, exact-evidence catalogue pages, saved run findings and
conversation finding lists use review priority before previews and pagination.
The first occurrence of each distinct finding comes first. Repeated identifier
readings and equivalent observations of the same source material follow in the
full list, with each original finding link intact. Ties use the newest run and
then the saved item order.

A short reason beside a promoted finding identifies the next check. Examples
include an address estimate used by another saved analysis, matching native
reply IDs, differences between investigator-selected location claims, contrary
material cited by a saved AI suggestion, and a timed interval of uncertain
speech. For identifier matches, the comparison checks up to 64 keys with
two to five distinct originals or attachments, starting with the least common.
Promotion requires readable, active sources and at least two different content
hashes. Address estimates are checked against the 32 most recent applicable
acceptances and up to two recent saved uses per acceptance. Findings outside
these contextual checks remain in the full list. Open a promoted identifier
to compare the matching sources, or a used estimate to open an analysis that
relied on it. If a page of conversation messages exceeds the identifier lookup
limit, opening **Message details** checks the selected message on its own.
These rules run locally on retained findings.

Timing-only message-link candidates from new runs are available in the saved run
and its download. Timing-only output is hidden by default in run results.
Previously catalogued timing-only findings are also hidden by default in the
catalogue, person findings and conversations. This applies only to known
deterministic pairs with a valid elapsed interval, explicitly no shared words,
and no native reply reference. Known lexical-only adjacency and AI topic
continuations stay in the saved run. Exact native reply candidates, qualifying AI
links and unknown historical shapes remain eligible findings.
Where timing-only rows are available, **Show all findings** reveals them and
preserves other filters. Exact result links reveal their target at its saved
index. Candidate links require investigator review.

Conversation readers show single-message findings in **Message details** and
session and multiple-source findings under **Conversation findings**. Saved
inferred sessions also supply the transcript colours and boundary markers.
They use the same latest-result and current-source checks as the catalogue,
matching individual finding citations to the reported participant pair.
The **Instances** tab shows a short excerpt from saved reports with verified
coverage of a complete grouping. **Full summary** reveals all saved statements
for that instance inline. Owners and editors can request missing
summaries for a specific instance. Mixed-method runs retain per-finding **AI
suggestion** labels. Finding pagination is independent of message pagination;
the reader starts no work. See [conversation reading](evidence.md#reading-conversations).

By default, the newest requested successful equivalent run supersedes earlier
findings before search filters. Equivalence includes method/version,
execution settings, meaningful parameters and ordered exact source versions/hashes,
excluding automatic-check bookkeeping/requester identity. Successful empty runs
supersede; failed, unsupported, insufficient, cancelled or pending runs do not.
**Result history → All saved results** exposes history. Different settings and
extraction versions stay distinct. Changed/missing sources show unavailable rows without finding content.
Saved runs display the address estimate they used and warn if it is no longer selected.

Finding search opens with filters collapsed. Rows lead with the finding or a
short preview, a source link and relevant qualifiers. Inferred session rows show
reported participants, message count and observed times. Bounded **Matching search
details** retain the exact matching text when it differs from the preview.
**Back to findings** restores the search, filters and page from the findings list.

AI findings display explicit reported-sender/recipient UUID references using the
names of participants in their cited communications. Source quotations and
downloaded retained results stay exact. Model requests keep participant IDs;
resolving display names adds no personal details to AI payloads. Reported
associations do not verify identity.

Finding headings and report participant filters use readable names. Session thread
references pair the export title with the exact native thread ID. Processing
bookkeeping IDs remain in downloaded results instead of the method assumptions;
call-pair tables show the participant names without repeating internal IDs.

Pattern reports show key **Report totals** before the record list. Supporting
calculations, selection details and method explanations remain under **About this
report**; pagination does not limit the totals' full selected scope.

Person filters use explicitly linked person locations and cited communication
senders/recipients. Other run sources, matching names and contact strings establish
no association. Person detail links to findings and related evidence.

The relational catalogue publishes with immutable results. Its additive backfill
reads saved JSON, commits/resumes per run, and neither reruns analysis nor reads
originals. Stop old writers/workers before migration, then restart updated services.
The timing-only migration updates only the existing `weak` projection flag and
reverses to its former plate-only meaning on rollback. Startup never rebuilds
projections. Core migrations 0009 and 0010 add review-priority fields and backfill
the existing catalogue from retained results, resuming by result and preserving
finding IDs, indexes and downloads.

**Case Tools → Repeated identifiers** groups exact retained candidate strings: emails,
phones, complete URLs, bare domains, explicit identifiers and non-weak plate text.
It preserves email local-part/identifier case, phone international-prefix status
and URL paths, lowercases domains/hosts, and compares plates as uppercase ASCII
letters/digits. URLs/domains remain separate kinds. Matches establish no identity,
ownership, association or independent corroboration.

Counts collapse repeated runs/extractions into distinct original/attachment
references; matching hashes warn of copies. Opening groups validates current-page
sources, retaining unavailable rows. Identifiers appear only after representative
source validation. Partial coverage stays visible. Extraction may stop at
2,000 identifier occurrences. Search values directly. **Filters** holds identifier
type, evidence type, person, and result history. An empty view does not establish
that no connection exists. Editors can start an identifier check from the empty
state. Linked extraction and identifier checks can extend coverage.

### Sighting groups

Open **Case Tools → Sighting groups** to save a proposal across retained
observations. Owners and editors choose one anchor, select its direct
connections, and can add supporting or contrary records. Viewers can inspect
the proposal and its review history. Saving requires at least one direct
connection. Select up to 12 direct connections from a single anchor or image
region, plus up to 12 records in each context role.

Each connection shows both original observations and its basis. **Explicit
source association** uses the reported person or vehicle on retained location
records. **Shared identifier** compares exact normalized plate candidates and
retains the original regions, readings, and competing variants. Matching file
checksums flag copies. **Time/place proximity** uses already-retained spatial
group or co-location findings with their original tolerances and position
sources. Visual similarity and general entity matching are unavailable.

Connections apply only to the displayed pair. They establish neither identity
nor a common event. Group decisions are recorded separately from source records.
Overlapping proposals can retain different decisions. The detail page links to
the original regions and observations, exact comparison versions, prior runs,
and alternative proposals.

Saving and reviewing groups use local database records and make no inference
or geographic service requests. Archived or superseded inputs leave a proposal
available as history with review closed. Missing or changed originals withhold
the result and its download. To use newer comparisons, start a new proposal.

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
`processing_version_unavailable`; start a new run. Earlier results are retained.

Single/bulk investigator uploads queue applicable native text extraction, image
metadata, printed-English OCR, plate search, audio metadata, available-text
identifiers/dates/keywords and location quality/address normalization. New call
transcripts/recordings process independently. Extraction follow-ups bind that exact
version. Unsupported/insufficient inputs remain visible outcomes.

Evidence readers list every current published finding that cites the selected
original or one of its attachments, 25 per page, in review-priority order.
Weak findings remain visible with their qualifiers. A finding citing several
sources appears once. Opening a finding and returning restores the evidence
collection and findings page. **Analysis history** links to checks that used
the evidence, including superseded results, reports, empty results, and failures.
Single-source summaries appear only after the retained input is verified.
Results covering a selection open with the full result and its citations.
Failed and cancelled work retains its status and recovery details.

**Processing** prioritizes reviews and recovery under **Needs attention** and
queued or running checks under **In progress**. **Needs attention** includes
stopped preparation even if it queued zero checks, stopped vehicle imports, and
current trip or address preparation failures. Each row leads to its recovery
options. Pending message reviews require the current CSV review format, and
returning from a review opened in that list restores its page. A newer session
summary request supersedes a stopped request for the same selected sessions.
**Browse findings** opens the case's findings. Expand **Processing
history** for saved results and earlier work. Open a selected batch to see all
its checks and return to that batch after inspecting a result. Batch preparation
includes extraction follow-ups.
See [progress and recovery](background-tasks.md#investigator-progress-and-completed-imports).

New uploads, manually added evidence, call additions and newly published CSV
rows queue applicable analysis in bounded background batches. A duplicate
submission recovers the existing evidence and batch. **Processing → Analysis
actions → Reprocess evidence** uses the same methods for selected active evidence
types, with all types selected initially. Originals and earlier results remain
available. See [background tasks](background-tasks.md#investigator-progress-and-completed-imports).

Deterministic methods run once. Configured private inference and case-approved
external inference run independently. Geographic lookups use the configured
service for each method. Local extraction and transcription prepare exact
retained results for text follow-ups. One candidate transcript supplies automatic
text analysis even when multiple transcribers were run. Service configuration and
external approval are checked during preparation, execution and follow-ups.
Unsupported inputs and provider failures leave independent results available.

Methods cover inspection and extraction, OCR, plates, transcription, identifiers,
writing and repeated wording, topics and claims, sessions and exchanges, location
quality and movement, and geographic lookup. Automatic comparisons can include
bounded relevant active same-case history. Results disclose the actual inputs.
Use **Choose analysis** for an explicitly defined writing comparison, an area
search, or a comparison of claimed events. Choose an address estimate before
using it in a spatial method.

Message and vehicle CSVs still require explicit interpretation. Analysis starts
from confirmed newly published rows; see [message imports](analysis-communications.md#message-csv-imports)
and [vehicle imports](vehicle-location-imports.md#checking-published-observations).

Source choices expose prerequisites/retained inspection status. Some require exact
extractions, transcripts or selected address estimates. Blocked sources show their
prerequisite and a preparation link when available. Selections that become
blocked stay visible with a removal action, and must be resolved before analysis.
**Not yet inspected** makes no format-support claim. Readiness checks local
configuration; execution checks live connectivity and identity.
[Retained synthesis results](synthesis.md) distinguish text/finding citations;
[chronology](chronology.md) shows reported times, acquisitions and separately cited unplaced claims;
[previews](original-previews.md) provide private PDF/text reading and WAV/M4A playback.

**Additional analysis** selects task, exact retained sources and context for
comparisons, lookup, model work or conversation imports. From an evidence record,
choose a compatible recording or retained extraction when the method requires one.
The picker shows 25 sources per page,
including retained extraction versions/call additions. Cursor navigation avoids
full-case counts/scans; paging/search preserves selections and permits removal
from other pages. Execution allows 500 sources within parser/inference limits;
only selected-operation settings are validated/saved.

**Read inferred message sessions**, **Inspect message timing** and **Summarize
message sessions** accept Text messages only. Call transcripts use **Inspect
keywords and topics** or **Suggest claims and commitments**. **Compare exchanges
across channels** also accepts calls/FaceTime. Earlier runs keep their selections.

On-demand analysis uses the same available services for the selected method.
External AI and geographic services require the case owner's approval in
[Case Settings](cases.md#external-analysis-approval). A private service failure
cannot enqueue a new external request. [Map address lookup](maps.md#local-address-lookup)
uses its dedicated private service flow.

Call/vehicle report rows and filters use user timezone, with explicit UTC-hour
analysis and missing-time warnings. Duplicate reports group byte hashes; trips
retain first/last observed links and complete paginated source inspection.

## Inspecting run results

Structured lists/mappings paginate independently at 200 values, retaining finding,
report and collection filters; complete downloads remain available. Parser,
inference and result limits still apply.

Results lead with the finding and its citations. Retained warnings stay visible
under **Limitations**, including for completed runs. **Details and history**
holds retained downloads, selected inputs, settings, earlier runs and authorized
rerun controls. Run details show the retained execution destination.

Findings lead with their result, key measurements and supporting evidence.
Secondary measurements and native references live under **Details**. Critical
status, coverage, reasons and interpretation remain visible; metadata inspections
retain their dedicated tables. Findings without a narrative or recognized
primary measurements keep a readable structured fallback.

Communication pairs show each message once, with reported participants, a
timezone-labelled timestamp and source link. Their heading states the elapsed
interval; nonempty shared wording and a concise limitation remain visible.
Duplicate Before/After payloads and repeated source quotations are omitted from
the reading view; exact anchors and native IDs remain inspectable in **Details**.

Model findings show **AI suggestion** in the evidence reader, Case Tools,
and individual findings. The findings browser marks
**AI-assisted run** because a
single run can combine observed keywords or timing with model suggestions.

- Image metadata distinguishes embedded/supplied claims. Image and document-page
  OCR results link [visible structure](analysis-extraction.md#visible-structure-in-images)
  to the words supporting each field. Plate results retain regions/crops,
  separate plate/OCR scores and low-score notices. Identifier
  normalization appears in **Details**.
- Document text retains native page/line anchors; recording intervals link to
  selected recording/transcript playback.
- Writing/style comparisons show both samples; model claims cite passages.
  Conversations/imports retain participants, labels, times and unresolved reasons.
- [CSV imports](analysis-communications.md#message-csv-imports) publish only after
  separate message/new-person review. Completed preview means ready to review.
- Location schematics distinguish reported positions, selected estimates and
  candidates; measurements/map references link to sources.

Runs show 50 findings at most; derived inputs link extraction and original.
Owners and editors can select or clear an address estimate from its finding. Saved
finding review reasons remain in the database for retention.

**Download results** includes private result/source/provenance JSON. Missing/changed
exact inputs withhold results/downloads. [Workers](background-tasks.md) execute
queued runs, distinguishing queued/running/complete/partial/no-findings/insufficient/
unsupported/failed/cancelled. Progress and completed results update in place with
JavaScript; **Refresh status** remains available without it. Runs retain checksums,
parameters, method, and anchors. Earlier output remains available after reruns.

Cancellation blocks publication but recalls no sent request. Workers require
currently active verified owners/editors and recheck sources before work, transfer
and atomic publication. Revoked access blocks work and inspection. Duplicate
delivery cannot reclaim running/completed runs; original submission IDs recover
the same run. Failed work needs a new run, preserving queue history.

**Run again** retains the selection and processing route. External reruns require
current case approval for the configured route. Confirm a crashed worker
stopped before cancellation/recovery; it may already have sent requests. Cancelled
output remains withheld. New runs/queue inserts commit or roll back together.

Originals/supplied metadata, extracted observations, suggestions and investigator
conclusions stay distinct. Using an estimate does not verify a location. Spatial
inputs require a selected geocode candidate. Downstream runs pin that selection.
Clearing it blocks new processing with the estimate and preserves historical
results.

## Supported operation guides

- [Extraction](analysis-extraction.md): formats, parser isolation, anchors, partial
  coverage and synthetic evaluation for documents/images/audio/OCR/transcription.
- [Communications](analysis-communications.md): text spans, sample adequacy,
  native/inferred sessions, participant review and supported iMessage CSV imports.
- [Locations](analysis-locations.md): gaps, grouping, dwell, proximity, co-location,
  geographic estimates and source limits.
- [Geographic context](analysis-geographic-context.md): feature candidates,
  historical snapshots, private downloads and configuration.

Tesseract English OCR is local, credential-free and never falls back remotely.
Handwriting/mixed profiles are unsupported; old results stay readable. Context
uses a separate reference service without AI credentials. Address-only miTrail
spatial analysis needs selected geocodes. Local grouping needs no external service.

## Local inference configuration

[Local analysis](local-analysis.md) owns methods, pinned identities, settings,
capacity, qualification and recovery. Private inference defaults; runs retain
routing/provenance. Queued drift fails recoverably without endpoint switching or
external fallback.

## External AI configuration

Connect OpenAI or Anthropic directly with an API key for text analysis. Pandect
also supports OpenAI and Workers AI through Cloudflare AI Gateway. External
recording transcription uses Workers AI through Cloudflare.

### Connect OpenAI or Anthropic directly

Set one of these keys in the ignored local `.env` or in the hosted web and
worker environments. Use the raw API key without an authentication prefix.

| Environment variable | Optional model override | Default model |
| --- | --- | --- |
| `OPENAI_API_KEY` | `PANDECT_OPENAI_MODEL` | [`gpt-6-luna`](https://developers.openai.com/api/docs/models/gpt-6-luna) |
| `ANTHROPIC_API_KEY` | `PANDECT_ANTHROPIC_MODEL` | [`claude-haiku-5-5`](https://platform.claude.com/docs/en/models/haiku-5-5/overview) |

With one direct key and no explicit `PANDECT_AI_TEXT_PROVIDER`, Pandect uses that
provider for external text analysis. If both keys are present, set
`PANDECT_AI_TEXT_PROVIDER=openai-direct` or
`PANDECT_AI_TEXT_PROVIDER=anthropic-direct` to enable a direct connection. An
explicit provider selection takes precedence over the keys. Keep credentials
server-side and match provider/model settings in web and worker.

Use a GPT model that supports Responses API JSON-object output, or a Claude
model that supports the Messages API. Direct OpenAI requests use
`store=false`, a 32,768-token completion allowance and a 300-second timeout.
Anthropic requests ask for a JSON object, with an 8,192-token allowance and a
300-second timeout. See the [OpenAI Responses API](https://developers.openai.com/api/reference/resources/responses/methods/create)
and [Claude Messages API](https://platform.claude.com/docs/en/api/messages/create).

### Use external AI

After editing the local `.env`, run `make run` to apply settings to Compose web
and worker. For [native development](development.md#run-python-on-the-host),
restart both processes with `uv run --locked --env-file .env`. For a hosted
installation, apply the same settings to both services.

The case owner reviews and approves configured destinations under
[Case Settings](cases.md#external-analysis-approval). New eligible ingest and
analysis requests then include the approved route.

### OpenAI with a key stored in Cloudflare

Store an OpenAI provider key in your authenticated gateway's **Provider Keys**
section. You can reuse an existing key. Set these values in the ignored local
`.env` or in both hosted environments.

```dotenv
PANDECT_AI_ACCOUNT_ID=your_32_character_cloudflare_account_id
PANDECT_AI_GATEWAY_ID=your_gateway_id
PANDECT_AI_GATEWAY_TOKEN=your_gateway_authentication_token
PANDECT_AI_TEXT_PROVIDER=openai
PANDECT_AI_TEXT_MODEL=auto
PANDECT_AI_OPENAI_KEY_ALIAS=default
```

Use raw tokens without a `Bearer ` prefix. `PANDECT_AI_OPENAI_KEY_ALIAS` selects
the stored provider key; change it if your key has a custom alias. Pandect sends
`cf-aig-authorization` and `cf-aig-byok-alias`, and deliberately omits the provider
`Authorization` header so Cloudflare supplies its stored OpenAI key. No OpenAI API
key or Workers AI token is needed in Pandect for this text route. See
[Cloudflare BYOK](https://developers.cloudflare.com/ai-gateway/configuration/bring-your-own-keys/)
and [OpenAI integration](https://developers.cloudflare.com/ai-gateway/usage/providers/openai/).

With `PANDECT_AI_TEXT_MODEL=auto`, Pandect selects the OpenAI model by task.
Case Settings shows the resolved model, and the run retains it.

| Text analysis | OpenAI model | Reasoning effort |
| --- | --- | --- |
| Topics with brief summaries | `gpt-6-luna` | Medium |
| Claims and commitments | `gpt-6-luna` | High |
| Message-session summaries | `gpt-6.1-terra` | Medium |
| Semantic conversation links | `gpt-6.1-terra` | High |
| Question-driven synthesis | `gpt-6.2-sol` | High |

An explicit model ID pins all OpenAI text tasks to that model. To switch an
existing single-model installation to task-based routing, set the value to
`auto` for both web and worker. If a queued run's reviewed model changes, it
requires case-owner approval before evidence can be sent to the new model.

OpenAI uses the Gateway's `/openai/responses` endpoint for these tasks.
Each request has a 32,768-token total completion allowance, including reasoning,
and a 300-second client timeout with the matching Cloudflare request-timeout
header. These bounds allow room for reasoning alongside the cited JSON output;
they are ceilings, not guaranteed usage or completion times. Reasoning tokens
are billed as output. See [OpenAI reasoning budgets](https://developers.openai.com/api/docs/guides/reasoning)
and [Cloudflare request timeouts](https://developers.cloudflare.com/ai-gateway/configuration/request-handling/).
Requests use `store=false` and JSON-object output. Existing source-quotation and
domain validation still apply. Refused, incomplete, malformed, or unsupported
output publishes no result.
An explicitly pinned GPT model must support these Responses API parameters.
Model availability/access and analysis
quality require live qualification. See [OpenAI JSON output](https://developers.openai.com/api/docs/guides/structured-outputs).

### Cloudflare configuration

| Environment variable | Purpose/default |
| --- | --- |
| `PANDECT_AI_ACCOUNT_ID` | Cloudflare account ID. |
| `PANDECT_AI_GATEWAY_ID` | Operator gateway ID. |
| `PANDECT_AI_GATEWAY_TOKEN` | Gateway authentication, sent as `cf-aig-authorization`. |
| `PANDECT_AI_TEXT_PROVIDER` | `workers-ai` selects Cloudflare Workers AI. `openai` selects OpenAI through Cloudflare. Workers AI is the default when no direct keys or explicit provider are set. |
| `PANDECT_AI_TEXT_MODEL` | Defaults to `auto`. OpenAI uses the task table above. Workers AI uses `@cf/meta/llama-3.3-70b-instruct-fp8-fast`. An explicit model ID pins the selected text provider to one model. |
| `PANDECT_AI_OPENAI_KEY_ALIAS` | Stored OpenAI provider key alias; `default`. |
| `PANDECT_AI_WORKERS_TOKEN` | Workers AI upstream credential; required only for Workers AI routes. |
| `PANDECT_AI_TRANSCRIPTION_MODEL` | `@cf/openai/whisper-large-v3-turbo`; external transcription remains on Workers AI. |

Workers AI uses `gateway.ai.cloudflare.com/v1/ACCOUNT/GATEWAY/workers-ai/MODEL`
and sends its separate upstream token in `Authorization`. Text inference uses a
4,096-token output allowance and temperature zero. OCR remains local Tesseract.
For external recording transcription, set the Cloudflare account, gateway and
gateway token plus `PANDECT_AI_WORKERS_TOKEN` in web and worker. Use a Cloudflare
API token with the account's Workers AI Read permission. Pandect checks the
transcription configuration before queueing a new attempt and again before
sending audio. Once the configuration is available, reopen the failed run and
approve a new attempt.
There is no provider switching or fallback. See
[Gateway authentication](https://developers.cloudflare.com/ai-gateway/configuration/authentication/)
and [Workers AI integration](https://developers.cloudflare.com/ai-gateway/usage/providers/workersai/).

### Limits, retention, and recovery

Workers AI uses a 60-second client timeout. All external AI routes have an
8 MiB request limit and 2 MiB response limit. Each HTTP attempt records case,
run, model, and bytes. Missing token usage stays unknown. Provider rate and
quota limits apply.

Match web/worker configuration. Pandect retains the provider, API format, any
stored-key alias, and inference settings without secrets. A changed execution
stops queued work recoverably. Approval binds to a case and named destination.
After revocation or a destination change, a case owner can review the new routes.
Start a new request to use newly approved routes. Retained results stay readable.

Review logging and retention with the provider and, if used, Cloudflare.
`store=false` for OpenAI and the Cloudflare skip-cache/no-log headers are subject
to each service's retention policies. Payloads, secrets and provider exception
bodies stay out of routine telemetry. Authentication, quota, throttling, timeout,
invalid output and missing configuration fail recoverably. HTTP 429/502/503/504
and Anthropic HTTP 529 get one retry with renewed access and destination checks.
Routine tests mock inference. Reports and deterministic checks need no AI
credentials.

## Data and code boundaries

`core.evidence.types` registers ten types; `core.evidence.queries` checks same-case
references. `core.analysis.reports` dispatches phone/geo reports; duplicates and
neutral helpers stay in core. [Architecture](architecture.md#dependency-boundaries)
owns composition/parser boundaries. `core.analysis.services` handles access,
snapshots, publication, and estimate selection. `operations` dispatches processors. UUID-only
tasks separate durable runs, results, and search records from disposable queue records.

Shared factories select domain forms; bases own selection/submission, processing
location/approval, archive and periods. Domain forms validate/serialize operations.
Source resolution reuses data within one validation pass, and search validates
shared runs once/request. Requests/publication recheck access/sources afresh;
reuse cannot preserve revoked access or a cleared estimate selection.

Core owns shared models; domain apps own processors. Follow the
[fresh baseline](architecture.md#primary-keys-and-the-initial-schema) and coordinate
PostgreSQL/private-storage backups. No global analysis admin/public storage exists.

## Deleting an imported source

Superusers with current case access confirm deletion through **More → Delete
evidence** or scoped admin. [Evidence deletion](evidence.md#permanent-deletion)
owns inventories, stale confirmation, dependency cascades, publication locking
and post-commit cleanup. Whole-case deletion removes analysis/context; unrelated
originals, people and vehicles remain outside source deletion.
