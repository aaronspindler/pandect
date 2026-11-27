# Case analysis and extraction

New evidence queues applicable checks. Owners and editors start and cancel work.
Viewers inspect originals and results. Case permissions apply to workers,
search, downloads and playback. See the
[interface guide](interface-design.md#case-tools-processing-and-additional-analysis)
for navigation.

## Findings and related records

**Case Tools → Latest records** previews up to three findings from the 40 most
recent eligible runs with published results. The preview uses the result's save
time and keeps the latest result per method and source or attachment.
Unavailable sources, weak OCR and timing-only findings are excluded. Open
**Findings** for the catalogue or **Processing** for results and run history.
For saved possible trips, use [Trips and the investigation map](maps.md).

**Browse all findings** searches retained labels, text and measurements.
Filters cover method, evidence type, exact evidence and person. Pages contain
25 findings. Person filters use linked locations and cited communication
participants. The evidence reader opens the exact-source filter. **Back to
findings** restores the search, filters and page. **Mark important** saves the
exact result in [Important](cases.md#important-items).

The newest requested successful equivalent run supersedes earlier findings
before filters apply, even if it found nothing. Equivalence includes method
version, execution settings, meaningful parameters, and ordered source versions
and hashes. **Result history → All saved results** opens earlier results.
Changed or missing sources show unavailable rows without finding content.

Finding lists rank distinct observations before repeats, preserving each original
link. Promoted findings explain the next review step, such as checking a native
reply, used address estimate, matching identifier or uncertain speech interval.
Identifier promotion checks up to 64 keys with two to five active originals or
attachments and at least two different content hashes. Address promotion checks
the 32 most recent applicable acceptances and up to two recent uses each. Other
findings remain in the list. **Message details** can check an individual message
when its page exceeds the identifier lookup limit.

New timing-only, lexical-only and broad AI topic links stay in the saved run.
Timing-only items are hidden by default, including qualifying historical
catalogue rows. **Show all findings** reveals those rows where available and
keeps other filters. Exact links open the saved item. Qualifying native replies,
cited AI suggestions and unknown historical formats remain eligible for the
catalogue. See the [session and exchange rules](analysis-communications.md#conversation-sessions-and-interaction).

New claim and semantic-link findings require a supported suggestion type and
substantive citations. Claims need a specific quoted event or action. Reference
and shared-event links need independent sources naming the same target, with at
least two distinct English content words or an alphanumeric identifier. A short
answer can qualify alongside a substantive cited question. Vague, hedged and
duplicate suggestions stay in Processing. Explicit corrections surface through
claims analysis and remain context in exchange comparisons. Exact quotations
establish source wording. Interpretations still require review.

Message timing, writing measurements and comparison intervals, keywords and
topics, and session summaries remain in **Processing** and downloads.
Complete and partial transcripts remain searchable. A timed speech finding
shows its playback interval and model uncertainty.

**Case Tools → Repeated identifiers** groups exact candidate emails, phones,
URLs, bare domains, explicit identifiers and non-weak plate text. Normalization
preserves email local-part and identifier case, phone international-prefix
status and URL paths. Domains and hosts use lowercase, and plates use uppercase
ASCII letters and digits. URLs and domains stay separate. Counts combine repeat
analyses of the same original or attachment. Matching hashes flag possible
copies. Opening a group validates its current-page sources and discloses
unavailable records or partial coverage. Extraction can stop at 2,000
occurrences. A match establishes shared wording and leaves identity, ownership
and source independence open.

**Case Tools → Narrative relationships** opens suggested leads and their
quotations. It is also linked from **Repeated identifiers**. Recording a
decision returns owners and editors to the reviewed row.

### Sighting groups

Open **Case Tools → Sighting groups**. Owners and editors choose one anchor
observation or image region, one to 12 direct connections, and up to 12
supporting and 12 contrary records. Viewers can inspect the proposal and
review history.

Connections use the reported person or vehicle on location records, exact
normalized plate candidates, or retained spatial group and co-location
findings. They preserve source observations, regions, competing plate readings,
copy warnings, tolerances and position sources. Each connection applies to one
pair and leaves identity and shared events unresolved. Decisions belong to the
proposal. Overlapping proposals can keep separate decisions.

Saving and reviewing use local records. Superseded inputs leave a historical
proposal with review closed. Archived, missing or changed originals withhold
the result and download. Restoring an archived original can make the result
available after verification. Start a new proposal to use newer comparisons.

## Evidence chat

A verified owner or editor can ask a question when the owner has enabled external
AI and approved the configured Cloudflare destinations. Evidence chat saves
the question, cutoff and processing coverage at submission. The
Cloudflare investigation Agent searches the private case index, follows material
leads, opens the original passages and uses the case's configured model through
AI Gateway. Published substantive claims link to quotations in eligible sources.
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

The Evidence text filter uses verified [Cloudflare AI Search](cloudflare-search.md)
matches, with one row per source record and a link to the matching passage.
Clear the query to browse and filter the retained collection without hosted search.

Reports cover calls, trips, location coverage and receipt delays, speed and
heading, and exact byte duplicates. They use active evidence. Saved trips build
in background chunks. Current-field reports stream matching rows in batches of
up to 500. Source lists show 25 per page, with separate pagination for participant
pairs. Totals, exact medians, duplicate counts and adjacent comparisons cover
the full selection. Call duplication uses the audio-byte hash. Unknowns and time
exclusions remain visible. Times use the account timezone with explicit UTC
labels for hourly analysis.

## Retained processing

New uploads, manually added evidence, call additions and newly published
[message CSV](analysis-communications.md#message-csv-imports) and
[vehicle CSV](vehicle-location-imports.md#checking-published-observations) rows
queue bounded background batches. Duplicate submissions recover the existing
evidence and batch. Deterministic checks run once. Each AI check uses its
owner-approved route and can include bounded relevant active case history.
Extraction follow-ups bind the exact extraction version. See the
[method guides](#supported-operation-guides).

**Processing** shows **Needs attention**, **In progress** and **Processing
history**. Open a batch for its checks and preparation, including follow-ups.
**Retry all failed checks** queues eligible failures. **Acknowledge missing
coverage** records who reviewed an individual failure and when. Acknowledged
failures remain in history and can be retried individually. Needs attention
also links to stopped preparation, imports, and trip or address work. See
[progress and recovery](background-tasks.md#investigator-progress-and-completed-imports).

**Processing → Reprocess evidence** applies the same checks
to selected active evidence types. An identical scope with the same execution
settings and approvals can have one active request. Earlier results remain in
history. Use **Case Tools → Analysis methods** or **Choose analysis** when a
method needs an explicit selection or interpretation.

The source picker shows 25 sources per page and keeps selections during
search and paging. It includes exact extractions and call additions. Blocked
sources explain their prerequisites and stay visible until resolved or
removed. **Not yet inspected** leaves format support unknown. The worker
allows up to 500 sources and 16 MiB of loaded material, with tighter method
limits. Only the selected method's settings are saved. The person-specific
[writing workflow](analysis-communications.md#writing-samples-by-person) reads
a dated selection in batches.

Topic inference can retain independently calculated keywords as a partial result.
Topic and claim runs can retain verified suggestions and count rejected citations.
Session summaries disclose missing or unusable ranges. Source/access changes
or a changed execution route stop publication. A run queued for an unavailable
method version fails recoverably. Start a new run in Processing.

The evidence reader shows cited findings and links to **Analysis history**,
including superseded checks, reports, empty results and failures. Findings with
several citations appear once for each relevant evidence record.

## Inspecting run results

Results lead with findings, key measurements, and cited evidence. **Limitations**
shows warnings and coverage. **Finding details** holds technical anchors and
secondary measurements. **Analysis run details** or **Details and history**
holds selected inputs, execution settings, earlier runs, downloads and rerun
controls. Runs show 50 findings per page. Structured values paginate separately
at 200 values.

Model findings show **AI suggestion**. The catalogue marks **AI-assisted run**
when a run can combine suggestions and deterministic results. Citations show
reported participant names, original quotations, export rows, and the provenance
of transcripts, extractions, estimates and prior findings. Downloads retain saved
wording and internal IDs. Historical OCR keeps its
[visible structure](analysis-extraction.md#visible-structure-in-images), and
plate findings keep source regions, private crops and score warnings.

Spatial findings link reported coordinates, selected address estimates and lookup
candidates to their sources. A saved run keeps the exact estimate it used and
warns if that estimate is no longer selected. Clearing an estimate prevents new
use. Coordinates and links remain available without tiles or JavaScript. See
[map privacy and fallback](maps.md#observation-maps-in-findings).

**Download results** saves private uncached JSON with results, inputs and
provenance. Archived, missing or changed exact inputs withhold dependent
results and downloads. Processing history retains the run and its status.
Progress updates in place, with a **Refresh status** fallback.

Workers check current verified edit access and exact source integrity before
work, transfer and publication. Cancellation prevents publication. Requests
already sent remain with their recipients. Duplicate delivery cannot reclaim
running or completed runs. **Run again** retains the inputs and route, which
must still have current approval. Confirm an interrupted worker stopped before
recovery. See [worker recovery](background-tasks.md#failure-inspection-and-recovery).

## Supported operation guides

- [Extraction](analysis-extraction.md): formats, parser isolation, anchors, partial
  coverage and synthetic evaluation for documents/images/audio/OCR/transcription.
- [Communications](analysis-communications.md): text spans, sample adequacy,
  native/inferred sessions, participant review, and CSV imports for iMessage and WhatsApp.
- [Locations](analysis-locations.md): gaps, grouping, dwell, proximity, co-location,
  geographic estimates and source limits.
- [Geographic context](analysis-geographic-context.md): feature candidates,
  historical snapshots, private downloads and configuration.

## External AI configuration

Pandect runs model-based checks only through Cloudflare AI Gateway. Configure the serving provider and gateway credentials on the web and worker services. Each case owner controls external AI and the material each destination may receive.

### Use external AI

After editing the local `.env`, run `make run` to apply settings to Compose web
and worker. For [native development](development.md#run-python-on-the-host),
restart both processes with `uv run --locked --env-file .env`. For a hosted
installation, apply the same settings to both services.

The owner enables external AI and approves the configured destinations in
[Case settings](cases.md#external-ai-and-hosted-search).

### Model choices for case owners

In [Case Settings](cases.md#external-ai-and-hosted-search), case owners can choose
OpenAI or Anthropic Claude models for each text task. With the gateway's OpenAI
key configured, **Image and scanned page text** and **Vehicle plate text** can
each use GPT-5.4 mini, GPT-6 Luna, GPT-6.1 Terra, or GPT-6.2 Sol. Both use the
installation's vision default unless the owner chooses a model. Recording
transcription has its own model choices and defaults to Whisper Large v3 Turbo.

Supported OpenAI Responses and Anthropic Messages models offer an **Effort**
setting for each text task through Cloudflare AI Gateway. The available
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
each task below. Those models can be selected for other text methods when configured.
Case Settings shows the resolved model, and the run retains it.

| Text analysis | Default OpenAI model | Reasoning effort |
| --- | --- | --- |
| Topic summaries and narrative relationships | `gpt-6-luna` | Medium |
| Claims and commitments | `gpt-6-luna` | High |
| Message-session summaries | `gpt-6.1-terra` | Medium |
| Semantic conversation links | `gpt-6.1-terra` | High |
| Evidence chat and retained question-driven synthesis | `gpt-6.2-sol` | High |
| Claim comparison | `gpt-6.2-sol` | Medium |

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
8 MiB request limit and 2 MiB response limit. Each HTTP attempt records its case, model, bytes and the
associated analysis run or Evidence Chat turn. Missing token usage stays unknown. Provider rate and quota
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

[Architecture](architecture.md#dependency-boundaries) owns app and parser
boundaries. In `core.analysis`, `services` manages runs, `source_selection` pins
exact versions, and `source_resolution` checks current integrity and
dependencies. The worker uses the `operations` registry and `result_validation`
before atomic publication. Saved results and the relational findings catalogue
publish together. Migrations can backfill projections from saved JSON without
reading originals or rerunning analysis. Startup never rebuilds projections.

## Deleting an imported source

Superusers with current case access confirm deletion through **More → Delete
evidence** or scoped admin. [Evidence deletion](evidence.md#permanent-deletion)
owns inventories, stale confirmation, dependency cascades, publication locking
and post-commit cleanup. Whole-case deletion removes analysis/context; unrelated
originals, people and vehicles remain outside source deletion.
