# Case analysis and extraction

The case **Analysis** workspace keeps current-field reports separate from retained
processing runs. Owners and editors can start/cancel processing and append reviews;
viewers inspect authorized reports, originals, and results. The case boundary also
applies to workers, search, result downloads, and inline recording playback.

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

On an evidence detail page, owners and editors choose **Analyze evidence** once
to queue all applicable local analysis for that item's type and retained content.
The evidence analysis page lists its processing runs and links to progress and
results. Uploading evidence alone does not start analysis. Originals and earlier
runs remain retained, and archived evidence can also be analyzed.

Local processing inspects supported file metadata, reads printed English in
JPEG/PNG images using Tesseract, extracts native document text,
and analyzes available original or supplied text without sending evidence to
external services. After image or native document extraction finishes, text analysis uses
that exact retained extraction version. Unsupported formats and insufficient
content remain explicit outcomes rather than claims that every method succeeded.

Additional model processing is a deliberate choice. Select the operation and
retained source, review its settings, and choose **Local** (the default) or
**Cloudflare AI Gateway**. Cloud processing requires explicit submission approval;
local failure never triggers cloud fallback. Geographic services require external
approval unless the operator explicitly designates the endpoint as local.
Local keyword analysis does not enable an AI summary automatically.
Context-dependent comparisons, area queries and conversation
imports still require deliberate source selection and assumptions through the
manual analysis workflow.

Choose **Start analysis**, select an operation and retained sources, and queue it.
The picker shows at most 500 choices; use its title search to narrow larger cases.
Select an exact extraction version to analyze derived text. Review grouping/time
assumptions before starting an investigation workflow.
Only the selected operation's settings are validated and saved. Report forms load
only the people or vehicle choices applicable to that report. Finding reviews
return to the reviewed finding, including its results page; invalid reviews retain
the entered decision and comment for correction. PostgreSQL selects the latest
review for each visible finding; the full review history remains retained and
available on the run page.

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

`evidence.types` is the shared ten-type registry; `evidence.queries` enforces
same-case relationships. `analysis.reports` computes current-field reports.
Concrete document/media/text/conversation/location modules perform bounded work;
`analysis.services` owns authorization, input snapshots, publication, and reviews.
`analysis.operations` lists concrete callables; the worker passes authorization
and inference dependencies explicitly for the operations that use them.
`analysis.tasks` is a thin UUID-only queue entrypoint. Durable runs/results/reviews
and extraction search passages survive operational queue housekeeping.

Source resolution reuses originals and prior results only within one validation
pass. Search similarly validates a shared run once per request. External requests
and publication still perform fresh authorization and source checks, so this
reuse does not preserve access or acceptance after it changes.

Apply additive analysis migrations with the normal upgrade workflow; existing
originals are retained. Back up PostgreSQL and private storage together. Whole-case
deletion removes its analysis/context alongside evidence. No global analysis admin
or public result storage endpoint is registered.
