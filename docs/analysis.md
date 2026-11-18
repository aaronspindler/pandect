# Case analysis and extraction

This guide owns analysis behavior, retained results and configuration; the
[interface guide](interface-design.md#case-tools-processing-and-additional-analysis)
owns navigation. Eligible evidence queues local checks automatically. Owners/editors
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
**All findings from this evidence** applies the exact-source filter. Archived
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

Evidence highlights, exact-evidence catalogue pages, saved run findings and
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
matching individual finding citations to the conversation/source filter rather
than merely matching a run's inputs. Mixed-method runs retain per-finding **AI
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

Finding filters are open initially. The useful finding preview leads each row;
bounded **Matching search details** retain the exact matching text when it differs
from that preview.
Result limitations are open initially. **Download results** is beside the title,
and **Run again** is outside technical processing details, with any required
external-transfer explanation and approval kept beside the rerun action.

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

**Case Tools → Connections** groups exact retained candidate strings: emails,
phones, complete URLs, bare domains, explicit identifiers and non-weak plate text.
It preserves email local-part/identifier case, phone international-prefix status
and URL paths, lowercases domains/hosts, and compares plates as uppercase ASCII
letters/digits. URLs/domains remain separate kinds. Matches establish no identity,
ownership, association or independent corroboration.

Counts collapse repeated runs/extractions into distinct original/attachment
references; matching hashes warn of copies. Opening groups validates current-page
sources, retaining unavailable rows. Identifiers appear only after representative
source validation. Partial coverage stays visible. Extraction may stop at
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
`processing_version_unavailable`; start a new run. Earlier results are retained.

Single/bulk investigator uploads queue applicable native text extraction, image
metadata, printed-English OCR, plate search, audio metadata, available-text
identifiers/dates/keywords and location quality/address normalization. New call
transcripts/recordings process independently. Extraction follow-ups bind that exact
version. Unsupported/insufficient inputs remain visible outcomes.

Evidence readers show up to twelve distinct highlights from the latest
equivalent results citing that evidence. They check up to 50 ranked candidates
for readable sources and usable content, omit weak OCR and timing-only
findings, and disclose when the check limit leaves the preview incomplete.
Processing retains every outcome. Batch preparation differs from execution and
includes extraction follow-ups; queued counts are not completion. Imports stay
in progress until deliberate review finishes. See [progress/recovery](background-tasks.md#investigator-progress-and-completed-imports).

Owners/editors use **Processing → Run more checks → Rerun all analysis** for active
evidence and all retained call additions at request time. Background steps prepare
25 sources at most, reporting progress, unavailable counts and queued checks.
Resubmitting recovers the request. Fresh requests from any owner/editor are rejected
while the existing case-wide batch is preparing checks or any of its checks are
queued/running. Processing explains that work is already in progress. Once that
work has finished, a fresh request starts another batch, preserving results.
Scoped import checks and selected reprocessing remain separate. Empty calls supply
no content. This runs
basic local checks; **Reprocess evidence** runs the broader suite. CSV publication
still requires timezone/vehicle confirmation and queues saved trips. Summaries
need no geocoding. For address-only map positions, choose a lookup estimate. Conversation imports
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
require **Choose analysis**; choose address estimates before spatial use.

Completed vehicle imports offer deliberate **Run local checks on
imported evidence**, using bounded groups and excluding duplicates; see
[background tasks](background-tasks.md#investigator-progress-and-completed-imports).
Confirmed CSV messages automatically queue the analysis chosen during import
review: **Local checks** (no models, the default), **AI** (one model suite), or
**Both** (local checks and that AI suite). AI uses Gateway when fully configured,
otherwise local/private inference; review shows the resolved destination.
Both does not duplicate model runs across backends. Gateway processing requires
explicit transfer approval. Execution keeps the selected route, with no runtime
fallback.
**Follow message analysis** opens the batch; reruns offer the same choice and
require renewed external approval. Model failures preserve imports and remain
recoverable. See
[CSV analysis](analysis-communications.md#message-csv-imports).

Source choices expose prerequisites/retained inspection status. Some require exact
extractions, transcripts or selected address estimates. **Not yet inspected** makes no
format-support claim. Readiness checks local configuration; execution checks live
connectivity/identity. Incompatible selections are never silently reduced.
[Retained synthesis results](synthesis.md) distinguish text/finding citations;
[chronology](chronology.md) separates observed/acquired times;
[previews](original-previews.md) provide private PDF/text reading and WAV/M4A playback.

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

Results preserve sources, processing details, and limitations.

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

- Image metadata distinguishes embedded/supplied claims. OCR/plates retain
  regions/crops, separate plate/OCR scores and low-score notices. Identifier
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

**Start another run with these inputs** retains selection but uses current settings.
Its requester reviews/approves external destinations; changed settings require
fresh review. Local reruns need no external approval. Confirm a crashed worker
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

The backend supports Workers AI and OpenAI GPT text analysis through Cloudflare
AI Gateway. Local processing remains the default; investigators must select and
approve external processing. Approval identifies the upstream provider and model.
Browsers receive no credentials, and the backend never contacts OpenAI directly.

### OpenAI with a key stored in Cloudflare

Store an OpenAI provider key in your authenticated gateway's **Provider Keys**
section. An existing key can be reused. Set these in the ignored local `.env`
(or identically in the hosted web and worker environments):

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

Run `make run` after changing Compose configuration; it updates web and worker.
For native development, restart both processes using `uv run --locked --env-file
.env ...`; Django itself does not load `.env`. Start a new analysis, choose the
external service, inspect its destination, and approve the selected transfer.
Changing configuration does not enable automatic cloud processing.

With `PANDECT_AI_TEXT_MODEL=auto`, Pandect selects the OpenAI model by task.
The approval screen shows the resolved model, and the run retains that choice.

| Text analysis | OpenAI model | Reasoning effort |
| --- | --- | --- |
| Topics with brief summaries | `gpt-6-luna` | Medium |
| Claims and commitments | `gpt-6-luna` | High |
| Message-session summaries | `gpt-6.1-terra` | Medium |
| Semantic conversation links | `gpt-6.1-terra` | High |
| Question-driven synthesis | `gpt-6.1-sol` | High |

An explicit model ID pins all OpenAI text tasks to that model. To switch an
existing single-model installation to task-based routing, set the value to
`auto` for both web and worker. If a queued run's reviewed model changes, it
requires fresh approval before evidence can be sent to the new model.

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

### Provider configuration

| Environment variable | Purpose/default |
| --- | --- |
| `PANDECT_AI_ACCOUNT_ID` | Cloudflare account ID; empty disables external inference. |
| `PANDECT_AI_GATEWAY_ID` | Operator gateway ID. |
| `PANDECT_AI_GATEWAY_TOKEN` | Gateway authentication, sent as `cf-aig-authorization`. |
| `PANDECT_AI_TEXT_PROVIDER` | `workers-ai` (default) or `openai`; text analysis only. |
| `PANDECT_AI_TEXT_MODEL` | Defaults to `auto`. OpenAI uses the task table above. Workers AI uses `@cf/meta/llama-3.3-70b-instruct-fp8-fast`. An explicit model ID pins the selected text provider to one model. |
| `PANDECT_AI_OPENAI_KEY_ALIAS` | Stored OpenAI provider key alias; `default`. |
| `PANDECT_AI_WORKERS_TOKEN` | Workers AI upstream credential; required only for Workers AI routes. |
| `PANDECT_AI_TRANSCRIPTION_MODEL` | `@cf/openai/whisper-large-v3-turbo`; external transcription remains on Workers AI. |

Workers AI uses `gateway.ai.cloudflare.com/v1/ACCOUNT/GATEWAY/workers-ai/MODEL`
and sends its separate upstream token in `Authorization`. Text inference uses a
4,096-token output allowance and temperature zero. OCR remains local Tesseract.
For external recording transcription, set `PANDECT_AI_WORKERS_TOKEN` in both the
web and worker environments, including when text uses OpenAI. Use a Cloudflare
API token with the account's Workers AI Read permission. Pandect checks the
transcription configuration before queueing a new attempt and again before
sending audio. Once the configuration is available, reopen the failed run and
approve a new attempt.
There is no provider switching or fallback. See
[Gateway authentication](https://developers.cloudflare.com/ai-gateway/configuration/authentication/)
and [Workers AI integration](https://developers.cloudflare.com/ai-gateway/usage/providers/workersai/).

Workers AI retains its 60-second client timeout. Both routes retain the 8 MiB
request limit and 2 MiB response limit. These bounds and inference controls are
application settings, not environment variables. Pandect imposes no daily
request-count limit. Each HTTP attempt records case/run/model and bytes; missing
token usage stays unknown.
Cloudflare and upstream provider rate/quota limits still apply. Usage records do
not imply an exact cost.

Match web/worker configuration. Provider, API format, stored-key alias and inference
settings are retained without secrets. Configuration changes stop queued work
recoverably. Old cloud jobs without a destination snapshot cannot move to OpenAI;
start a deliberate rerun with fresh approval. Retained results stay readable.
Initial analysis, synthesis, bulk processing and reruns bind external approval to
the reviewed destination; changed settings require renewed review.

Queued OpenAI work approved under the previous low-effort/8,192-token settings
stops recoverably after this upgrade. Start a deliberate rerun with fresh approval
to use the new settings; retained results remain readable. Local Ollama settings
are unchanged, with thinking disabled.

Queued work approved under the former daily request cap needs a deliberate rerun
with fresh approval after upgrading. Historical usage and failed runs are retained;
removing the cap does not automatically retry failed work.

Deliberately configure Cloudflare and OpenAI logging/retention. `store=false` and
the skip-cache/no-log headers do not override provider policies or guarantee zero
retention. Payloads, secrets and provider exception bodies stay out of routine
telemetry. Authentication, quota, throttling, timeout, invalid output and missing
configuration fail recoverably. HTTP 429/502/503/504 gets one transport retry with
renewed authorization and destination checks; other failures need deliberate
reruns. Routine tests mock inference and establish neither live availability nor
model quality. Reports and deterministic checks need no AI credentials.

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
