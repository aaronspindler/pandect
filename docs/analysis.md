# Case analysis and extraction

This guide owns analysis behavior, retained results and configuration; the
[interface guide](interface-design.md#case-tools-processing-and-additional-analysis)
owns navigation. Newly added evidence queues applicable analysis automatically. Owners/editors
start and cancel work. Viewers inspect originals and results. Case permissions apply to workers, search, downloads and playback.

Under **Case Tools → Latest records**, findings are dated by when their
result was saved. The preview selects up to three findings from the 40 most
recently created eligible runs with published results. It keeps the latest
result per operation and source or attachment, and excludes archived or
unavailable sources, weak OCR, and timing-only findings. Open
**Findings** for the catalogue or **Processing** for full results and run history.
**Case Tools → Trips** shows saved possible trips, source observations, and
location coverage alongside the map. Vehicle details preview the same saved
trips. See [Trips and the investigation map](maps.md).

**Browse all findings** searches retained labels, text and measurements, with 25
findings per page and filters for method, evidence type, specific evidence, and person.
The evidence reader applies the exact-source filter directly. The catalogue uses
active sources and labels weak plate regions. An empty catalogue links to
Processing. An empty search or filter selection offers to show all findings.

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

[Conversation findings](evidence.md#reading-conversations) use the same
latest-result and current-source checks as the catalogue. Exact citations match
findings to the reported participant pair. Mixed-method runs retain per-finding
**AI suggestion** labels. See the [grouping and summary rules](analysis-communications.md#conversation-sessions-and-interaction).

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
**Mark important** on an individual finding saves a reference to that exact
retained result, with shared investigator notes in [Important](cases.md#important-items).

AI findings display reported participant names for UUID references in their cited
communications. Pandect resolves those names for display. Source quotations and
downloaded results stay exact. See [what text analysis sends](analysis-communications.md#supported-text-operations).

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
or geographic service requests. Superseded inputs leave a proposal available
as history with review closed. Archived, missing or changed originals withhold
the result and its download. Restoring the archived originals makes the saved
result available again if its sources can still be verified. To use newer
comparisons, start a new proposal.

Private uncached result/provenance/reference JSON downloads retain `.json` names
and JSON bytes, using `application/octet-stream` plus attachment disposition for
reliable browser saves, including Linux WebKit. Image previews remain inline JPEG.
The [roadmap](evidence-analysis-roadmap.md) separates future work from current guides.


## Evidence chat

A verified owner or editor can ask a question when the owner has enabled external
AI and approved the configured Cloudflare destinations. Case Chat records the
question, original cutoff and case processing coverage at submission. The
Cloudflare investigation Agent searches the private case index, follows material
leads, opens the original passages and uses the case's configured model through
AI Gateway. Each substantive claim links to a quotation in an eligible source.
A complete review enumerates the source population and tracks reviewed entries
and gaps. Queued and failed checks leave Chat available.

Case members can read published replies and open their sources while external AI
is disabled. Changed or archived sources can withhold a reply. Updating a dated
answer creates a linked turn with a fresh cutoff. An investigator can approve a
proposed native PDF extraction and resume the original question against the same
cutoff. The approved result carries its own publication time.

See the [Cloudflare investigation Worker guide](cloudflare-agent.md) for the
private callback, worker configuration, budgets and interrupted-delivery recovery.

## Reports and search

Evidence search covers original Text/messages, every supplied call transcript and
completed/partial extraction passages. Call additions never multiply parent rows;
matches open the exact addition/extraction version. Case/type/archive/pagination
boundaries apply; HTML-like content displays as text.

An extraction whose run selected archived evidence is excluded from results and
counts. One bounded excerpt query considers ten matching passages per current-page
record, validating source, checksum and attachment. Invalid newer runs can fall
back to older candidates within that limit.

Reports cover call counts/durations/timing, trips, observation coverage/receipt
delays, speed/heading and exact byte duplicates. Reports use active evidence.
Trips process active histories in
background chunks and paginate complete trips. Other reports stream all matching
case, participant, vehicle and time rows in fetches of 500 or fewer. Source lists
show 25/page; totals, exact medians, duplicate groups and adjacent comparisons
remain complete. Participant pairs paginate separately. Duplicate occurrences
retain full group counts across pages; CSV matches link to full paginated selections.
Unknowns, time exclusions and originals remain visible.

These read-only current-field reports are calculated per request, separately from
saved trips. Call audio uses audio-byte hashes, distinct from manifest hashes.
Unknown duration/speed differs from zero. Gaps do not establish inactivity; repeated
addresses do not establish visits.

## Retained processing

`text_topics` and `session_summaries` retain stage warnings/combined
outcomes: keywords remain useful without inferred topics; missing session content
stays insufficient/partial. Topic response/quotation validation failures and
inference service failures retain available keyword findings as a partial result,
with a safe explanation and rerun guidance. Topics and claims can also retain
suggestions with exact source matches as a partial result when other citations
fail validation. See [communication analysis](analysis-communications.md) for
the text limits and citation rules. Source/access changes and runtime/model
identity drift still stop the run. Pending version 1 fails with
`processing_version_unavailable`; start a new run. Earlier results are retained.
Session and timing methods use version 2 and session summaries use version 3
for delayed replies and exact saved-session ranges. Pending runs on previous
method versions can be requested again in Processing.

Single/bulk investigator uploads queue applicable document text extraction, image
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
queued or running checks under **In progress**. Owners and editors can select
**Retry all failed checks** to request new attempts for eligible failures
across the case. On an individual failed check, **Acknowledge missing coverage** records the investigator and time. Acknowledged failures stay in history and can be retried individually. Bulk retries skip them. **Needs attention** also includes
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
types, with all types selected initially. An identical scope with the same
execution settings and approvals can have one active reprocessing request.
Originals and earlier results remain available. See
[background tasks](background-tasks.md#investigator-progress-and-completed-imports).

Deterministic methods run once. Each model check uses an
[owner-approved Cloudflare AI Gateway route](cases.md#external-ai-and-hosted-search) when configured and approved
for that method. Geographic lookups use the configured service for each method.
Native extraction, approved image recognition and transcription prepare retained results
for text follow-ups. Preparation, execution and follow-ups recheck service
configuration and external approval. Unsupported inputs and provider failures
leave independent results available.

Methods cover inspection and extraction, OCR, plates, transcription, identifiers,
writing and repeated wording, topics, claims and candidate narrative relationships,
sessions and exchanges, location
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

Open **Case Tools → Analysis methods** to choose a task and select the retained
sources and context for a comparison, lookup, or model check. From an evidence record,
choose a compatible recording or retained extraction when the method requires one.
The picker shows 25 sources per page,
including retained extraction versions/call additions. Cursor navigation avoids
full-case counts/scans; paging/search preserves selections and permits removal
from other pages. Execution allows up to 500 sources and up to 16 MiB of loaded
material. Pandect checks method-specific text limits before loading full bodies.
Only the selected operation's settings are validated and saved.

**Read inferred message sessions**, **Inspect message timing** and **Summarize
message sessions** accept Text messages only. Call transcripts use **Inspect
keywords and topics** or **Suggest claims and commitments**. **Compare exchanges
across channels** also accepts calls/FaceTime. Earlier runs keep their selections.

**Compare a claim** checks one quoted message or call transcript passage against
selected case evidence through the approved Cloudflare AI Gateway text model. It preserves
exact citations, unresolved material and matching source checksums. See
[claim comparison](analysis-communications.md#compare-a-claim) for supported
inputs, bounds and interpretation.

On-demand AI analysis and claim comparison use the case’s approved model for the selected task.
External AI and externally configured geographic services require the case
owner's approval in [Case Settings](cases.md#external-ai-and-hosted-search).
Explicitly private geocoding and geographic context use normal case edit access.
Private service failures stay on their selected route. [Map address lookup](maps.md#local-address-lookup)
uses its dedicated private service flow.

Call/vehicle report rows and filters use user timezone, with explicit UTC-hour
analysis and missing-time warnings. Duplicate reports group byte hashes; trips
retain first/last observed links and complete paginated source inspection.

## Inspecting run results

Structured lists/mappings paginate independently at 200 values, retaining finding,
report and collection filters; complete downloads remain available. Parser,
inference and result limits still apply.

Run overviews lead with findings and citations. Retained warnings stay visible
under **Limitations**, including for completed runs. **Details and history**
holds downloads, selected inputs, settings, earlier runs and authorized rerun
controls. Run details show the retained execution destination.

Individual findings, including single-finding runs, lead with the saved result
and key measurements. **Cited evidence** puts quotations and other cited
material beside their source links. Imported messages show their recorded export
row when available. Supplied transcripts, saved extractions, geographic
estimates and quotations from prior findings carry their own attribution.

Keep consequential status, coverage and uncertainty in view. When a saved AI
suggestion cites wording that matches the retained material, the finding shows
that match and identifies the interpretation for review. **Finding details**
holds secondary measurements, technical anchors and retained status fields.
**Analysis run details** holds full retained warnings and the run's inputs,
settings, downloads, history and authorized rerun controls. Metadata inspections
keep their dedicated tables, and findings without a narrative or primary
measurements keep readable structured values.

Communication pairs show each message once, with reported participants, a
timezone-labelled timestamp and source link. Their heading states the elapsed
interval. Shared wording and a concise limitation remain visible when
applicable. **Finding details** retains exact anchors and native IDs.

Model findings show **AI suggestion** in the evidence reader, Case Tools,
and individual findings. The findings browser marks
**AI-assisted run** because a
single run can combine observed keywords or timing with model suggestions.
Claim findings and session summaries display the reported sender's name when
the cited source identifies that participant. Saved wording, quotations and
citations are retained.

- Image metadata distinguishes embedded/supplied claims. Image and document-page
  Historical OCR results link [visible structure](analysis-extraction.md#visible-structure-in-images)
  to the words supporting each field. Plate results retain regions/crops,
  separate plate/OCR scores and low-score notices. Identifier normalization
  appears in **Finding details** on individual findings.
- Document text retains native page/line anchors; recording intervals link to
  selected recording/transcript playback.
- Writing/style comparisons show both samples; model claims cite passages.
  Conversations/imports retain participants, labels, times and unresolved reasons.
- [CSV imports](analysis-communications.md#message-csv-imports) publish only after
  separate message/new-person review. Completed preview means ready to review.
- **Selected observation positions** links reported coordinates, selected
  address estimates and lookup candidates to their sources. The configured
  map background loads automatically and displays those positions as circles,
  squares and diamonds. Coordinates and source links remain available without
  tiles or JavaScript. See [map privacy and fallback](maps.md#observation-maps-in-findings).

Runs show 50 findings per page. Derived inputs link to the extraction and original.
Owners and editors can select or clear an address estimate from its finding. Saved
finding review reasons remain in the database for retention.

**Download results** includes private result/source/provenance JSON. Archived,
missing or changed exact inputs withhold results and downloads. Saved work remains
in Processing history. [Workers](background-tasks.md) execute
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

Originals and supplied metadata, extracted observations, suggestions, and
investigator conclusions stay distinct. Spatial methods can use reported
coordinates directly. To use a derived address estimate, select a geocode
candidate. Downstream runs pin that selection. Clearing it blocks new processing
with the estimate and preserves historical results. An estimate does not verify
a location.

## Supported operation guides

- [Extraction](analysis-extraction.md): formats, parser isolation, anchors, partial
  coverage and synthetic evaluation for documents/images/audio/OCR/transcription.
- [Communications](analysis-communications.md): text spans, sample adequacy,
  native/inferred sessions, participant review, and CSV imports for iMessage and WhatsApp.
- [Locations](analysis-locations.md): gaps, grouping, dwell, proximity, co-location,
  geographic estimates and source limits.
- [Geographic context](analysis-geographic-context.md): feature candidates,
  historical snapshots, private downloads and configuration.

Native document decoding, image and recording metadata, and geographic grouping run in Pandect. OCR sends bounded, metadata-free JPEGs to the approved vision model through Cloudflare AI Gateway. PDF scanning skips pages with usable native text. Each extracted text records the original checksum, the submitted image checksum, and its original image or page. OCR and plate readings remain unverified model suggestions.

## External AI configuration

Pandect runs model-based checks only through Cloudflare AI Gateway. Configure the serving provider and gateway credentials on the web and worker services. Each case owner controls external AI and the material each destination may receive.

### Use external AI

After editing the local `.env`, run `make run` to apply settings to Compose web
and worker. For [native development](development.md#run-python-on-the-host),
restart both processes with `uv run --locked --env-file .env`. For a hosted
installation, apply the same settings to both services.

The case owner reviews and approves configured destinations under
[Case Settings](cases.md#external-ai-and-hosted-search) and selects
[External AI](cases.md#external-ai-and-hosted-search) for new eligible AI checks.

### Model choices for case owners

In [Case Settings](cases.md#external-ai-and-hosted-search), case owners can choose
OpenAI or Anthropic Claude models for each text task. Recording transcription
has its own model choices and defaults to Whisper Large v3 Turbo.

Supported OpenAI Responses and Anthropic Messages models offer a per-task
**Effort** setting through Cloudflare AI Gateway. The available
levels depend on the model. **Default** uses the task's configured effort or the
provider default. Higher effort can increase latency and token usage. New
requests use the saved effort and retain it with the execution settings.
See the [Claude effort guide][claude-effort] for how Claude applies effort.

The picker shows the resolved installation default. Previously saved models
outside the picker’s current list remain assigned while their routes are
configured.

[claude-effort]: https://platform.claude.com/docs/en/build-with-claude/effort

Pandect ships a release snapshot of the
[Cloudflare model catalog](https://developers.cloudflare.com/ai/models/).
Opening settings and searching its model list work offline. Available choices
depend on the task, supported request format and installation configuration.
Account entitlement, quota and provider availability are checked by the service
at request time.

For a release refresh, check out the intended revision of
[Cloudflare's documentation source](https://github.com/cloudflare/cloudflare-docs),
then run `python scripts/update-cloudflare-models.py /path/to/cloudflare-docs`
from Pandect. Review the generated catalog diff, including serving providers,
model IDs and request formats. The bundled catalog records its source revision.

| Environment variable | Additional model choices |
| --- | --- |
| `PANDECT_AI_TEXT_MODEL_CHOICES` | Comma-separated IDs for the configured OpenAI or Anthropic text provider, through AI Gateway. |
| `PANDECT_AI_TRANSCRIPTION_MODEL_CHOICES` | Comma-separated IDs for external recording transcription through Workers AI. |

Set the same configuration in web and worker. For custom model IDs, use models
qualified for the configured provider and request format. If a saved model is
retired, its task requires a new selection. Previously saved results retain
their original model and execution details.
Bundled Workers AI speech models use their supported audio adapter when selected
or configured as an installation default or choice. Custom compatible models
and the default Whisper Turbo route use the Workers AI JSON adapter.

### Using additional Cloudflare providers

Set the Cloudflare account, gateway ID and gateway token. For the account-level
AI REST API, supply a token with **Account → Workers AI → Read** in
`PANDECT_AI_WORKERS_TOKEN`. Choose how the installation authenticates to the
actual serving provider.

- For stored provider keys, configure the `default` alias on the gateway for
  each additional provider, then list its serving-provider ID in
  `PANDECT_AI_STORED_KEY_PROVIDERS` (for example, `anthropic`).
  The existing OpenAI provider-native connection supports a named key alias.
- To allow Cloudflare credits, explicitly set `PANDECT_AI_UNIFIED_BILLING=true`
  and configure the account's funding and limits in Cloudflare. This setting
  allows billed routes. It does not fund the account. Cloudflare's credential
  precedence applies when a default stored key exists. Key-only requests require
  stored credentials and cannot fall through to credits.

Pandect selects the configured gateway and serving provider on requests. The
model author can differ from the service receiving the request. Case owners
review the recipient and material before first use. A case may authorize text
and source references while leaving recording transfers unapproved. Consult the
[AI REST API](https://developers.cloudflare.com/ai-gateway/usage/rest-api/) and
[Cloudflare billing and credential rules](https://developers.cloudflare.com/ai-gateway/features/unified-billing/).

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

With `PANDECT_AI_TEXT_MODEL=auto`, Pandect defaults to the OpenAI model for
each task below. All three models are available choices for each text method.
Case Settings shows the resolved model, and the run retains it.

| Text analysis | Default OpenAI model | Reasoning effort |
| --- | --- | --- |
| Topic summaries | `gpt-6-luna` | Medium |
| Claims and commitments | `gpt-6-luna` | High |
| Message-session summaries | `gpt-6.1-terra` | Medium |
| Semantic conversation links | `gpt-6.1-terra` | High |
| Evidence chat and retained question-driven synthesis | `gpt-6.2-sol` | High |

An explicit model ID sets one default for all OpenAI text tasks. To use
task-based defaults, set the value to `auto` for both web and worker.

OpenAI uses the Gateway's `/openai/responses` endpoint for these tasks.
Each request has a 32,768-token total completion allowance, including reasoning,
and a 300-second client timeout with the matching Cloudflare request-timeout
header. These bounds allow room for reasoning alongside the cited JSON output;
they are ceilings, not guaranteed usage or completion times. Reasoning tokens
are billed as output. See [OpenAI reasoning budgets](https://developers.openai.com/api/docs/guides/reasoning)
and [Cloudflare request timeouts](https://developers.cloudflare.com/ai-gateway/configuration/request-handling/).
Requests use `store=false` and JSON-object output. Pandect applies source-quotation
and domain validation before accepting suggestions. Refused, incomplete,
malformed, or unsupported replies cannot supply suggestions. Some methods can
still publish independently calculated results, such as topic keywords, with
[partial coverage](#retained-processing).
An explicitly pinned GPT model must support these Responses API parameters.
Model availability/access and analysis
quality require live qualification. See [OpenAI JSON output](https://developers.openai.com/api/docs/guides/structured-outputs).

### Cloudflare configuration

| Environment variable | Purpose/default |
| --- | --- |
| `PANDECT_AI_ACCOUNT_ID` | Cloudflare account ID. |
| `PANDECT_AI_GATEWAY_ID` | Operator gateway ID. |
| `PANDECT_AI_GATEWAY_TOKEN` | Gateway authentication, sent as `cf-aig-authorization`. |
| `PANDECT_AI_TEXT_PROVIDER` | `workers-ai` selects Cloudflare Workers AI. `openai` selects OpenAI through Cloudflare. Workers AI is the default. |
| `PANDECT_AI_TEXT_MODEL` | Defaults to `auto`. OpenAI uses the task table above. Workers AI uses `@cf/meta/llama-3.3-70b-instruct-fp8-fast`. An explicit model ID sets a single default for the selected text provider. |
| `PANDECT_AI_OPENAI_KEY_ALIAS` | Stored OpenAI provider key alias; `default`. |
| `PANDECT_AI_WORKERS_TOKEN` | Account token for Workers AI and catalog routes using Cloudflare's account-level AI REST API. |
| `PANDECT_AI_VISION_MODEL` | OpenAI Responses-compatible vision model for OCR and plate recognition. Defaults to `gpt-4.1-mini`. Configure the gateway OpenAI key and select OpenAI as the text provider or include `openai` in stored key providers. |
| `PANDECT_AI_TRANSCRIPTION_MODEL` | Installation default for external transcription on Workers AI, initially `@cf/openai/whisper-large-v3-turbo`. |

Workers AI uses `gateway.ai.cloudflare.com/v1/ACCOUNT/GATEWAY/workers-ai/MODEL`
and sends its separate upstream token in `Authorization`. Text inference uses a
4,096-token output allowance and temperature zero. Vision uses a gateway-stored OpenAI key and a Responses-compatible vision model.
For Workers AI recording transcription, set the Cloudflare account, gateway and
gateway token plus `PANDECT_AI_WORKERS_TOKEN` in web and worker. Use a Cloudflare
API token with the account's Workers AI Read permission. Pandect checks the
transcription configuration before queueing a new attempt and again before
sending audio. Once the configuration is available, reopen the failed run and
start a new attempt.
Other speech routes accept the prepared WAV chunks in the format supported by
their endpoint. Pandect keeps supplied segment timestamps. For word-timed
replies, it groups speech into readable phrases at sentence endings, pauses
and short intervals, using the original word times as anchors. Text-only
replies retain the original chunk boundaries and show partial coverage. See
[Gateway authentication](https://developers.cloudflare.com/ai-gateway/configuration/authentication/)
and [Workers AI integration](https://developers.cloudflare.com/ai-gateway/usage/providers/workersai/).

### Limits, retention, and recovery

Workers AI uses a 60-second client timeout. All external AI routes have an
8 MiB request limit and 2 MiB response limit. Each HTTP attempt records its case,
model, bytes, and the associated analysis run, Evidence Chat turn, or meaning
search operation. Missing token usage stays unknown. Provider rate and quota
limits apply.

Match web/worker configuration. Pandect retains the provider, API format, any
stored-key alias, and inference settings without secrets. A changed execution
stops queued work recoverably. Approval binds to a case and named destination.
After revocation or a destination change, a case owner can review the new routes.
Start a new request to use newly approved routes. Retained results stay readable.

Review logging and retention with the provider and, if used, Cloudflare.
`store=false` for OpenAI and the Cloudflare skip-cache/no-log headers are subject
to each service's retention policies. Payloads, secrets and provider exception
bodies stay out of routine telemetry. Authentication, quota, throttling, timeout,
invalid output and missing configuration fail recoverably. Throttling and
transient HTTP 502/503/504 errors, including Anthropic HTTP 529, get one retry
with renewed access and destination checks. Exhausted quota stops the attempt.
Routine tests mock inference. Reports and deterministic checks need no AI
credentials.

## Data and code boundaries

`core.evidence.types` registers ten types; `core.evidence.queries` checks same-case
references. `core.analysis.reports` dispatches phone/geo reports; duplicates and
neutral helpers stay in core. [Architecture](architecture.md#dependency-boundaries)
owns composition/parser boundaries. `core.analysis.services` owns run submission
and cancellation. `source_selection` records exact versions of selected originals
and derivatives, and `source_resolution` reopens them with current integrity and
dependency checks. `worker` rechecks access and approval, dispatches the processors
registered in `operations`, validates their output through `result_validation`, and
publishes results and follow-ups. `address_estimates` owns estimate decisions and
checks their current revisions. UUID-only tasks separate durable runs, results, and
search records from disposable queue records.

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
