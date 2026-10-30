# Evidence analysis implementation roadmap

**Proposed priorities, 2026-10-27.** This roadmap turns the
[scored analysis catalogue](evidence-analysis.md) into an implementation sequence.
It proposes future work; it does not implement or schedule these features.

Start by making retained text searchable. Then make file contents searchable,
build useful conversation and movement workflows, and connect their results.
Add specialized matching where a case need and evaluation justify it.

## How priorities were chosen

Investigative value, ease, prerequisites, and reuse determine the order. The
catalogue's two scores remain separate: multiplying them would obscure a
high-value feature's dependency on a smaller enabling feature. Its 88 entries
also include overlapping capabilities; implement shared methods once.

The default assumes a general investigation workload, without measured evidence
volumes or a committed team capacity. There are no calendar estimates. Ship
bounded parts of large catalogue rows: native PDF text before broad document
OCR, and printed-text OCR before handwriting. The full-row scores below are
context, not new estimates for those smaller parts.

| Priority | Investigator outcome | Main work | Why this position |
| --- | --- | --- | --- |
| **1. Search and inspect existing material** | Find relevant text and understand recorded activity immediately. | Existing-text/transcript search; call statistics; vehicle coverage/receipt delays; exact-file duplicate candidates. | Uses retained data and the existing interface. Call statistics and receipt-delay analysis are **value 4 / ease 5**. |
| **2. Extract searchable file content** | Search documents, images, and recordings with links to the original passage. | Minimal retained results and background processing; native document text and media metadata; Gateway integration, transcription, then printed-text OCR. | Unlocks later workflows. Transcription is **5/3**; the full document/OCR capabilities are **5/2**. |
| **3. Build investigation workflows** | Understand conversations, candidate drives/visits, and observable writing patterns. | Parallel conversation, location, and descriptive writing-style tracks. | Native conversations are **5/2**, sessions **4/3**, drives/visits **4/2**, descriptive style **3/3**. Each track starts when its own inputs are ready. |
| **4. Connect evidence and synthesize findings** | Follow identifiers and events across sources, then ask questions with inspectable answers. | Candidate entity links, source-backed timeline, semantic retrieval, duplicate-source review, cited summaries and claim comparisons. | Links, timelines, and source-backed synthesis score **5/2**, but need dependable inputs and provenance first. |
| **5. Add targeted specialist tools** | Resolve particular visual, document, or audio review problems. | Plates, near-duplicate images, structured tables/transactions, and selectively face grouping/comparison, translation, or speaker separation. | Add the tools that fit observed case needs. Plates/faces are **4/2**; image duplicate comparison is **4/3**. |
| **6. Keep research-sensitive work conditional** | Explore difficult attribution or new modalities with demonstrated limits. | Candidate authorship, reference-voice comparison, signature comparison, landmark inference, manipulation triage, and video. | Evaluation or missing inputs dominate; these should not delay useful core investigation tools. |

Priorities are a default order, not a chain of global blockers. In particular,
the location and writing tracks can proceed while transcription/OCR coverage
expands. Specialist plate analysis can move earlier for a vehicle-heavy case
once image processing and source-region support exist.

## 1. Search and inspect existing material

**First implementation: extend the case Evidence collection search to retained
text.** Search Text bodies, Text message bodies, and all supplied transcripts
on Phone call and FaceTime attachments. Current collection search covers
metadata and related records, but not those contents. Preserve case scoping,
type/archive filters, pagination, and private responses. Return each call once
even if several additions match, and identify the matching transcript addition.

This is literal content search, an enabling capability narrower than the
catalogue's semantic search. It needs no AI, parser, or analysis framework.
Uploaded file contents remain outside search until extraction is implemented.

Next, ship small read-only reports from existing fields:

- Call counts, reported-duration distributions, and recorded timing patterns.
- Vehicle-observation coverage, missing/out-of-order observations, receipt delays,
  and reported-speed summaries. Show source rows and coverage windows.
- Exact-file duplicate candidates using file-byte checksums, including retained
  call audio where appropriate. Communication-record and attachment-record
  checksums have different meanings; use the audio-byte checksum for audio.

Keep these as queries over current records. Introduce stored results when an
extraction or retained analytical finding actually needs them. Repeated addresses
are not visits, missing observations are not inactivity, and unknown durations
or speeds are not zero.

**Complete when:** an investigator can find a phrase in every retained text
source, open the matching original/addition, and inspect the records behind a
report. Access revocation, archived material, duplicate attachment matches, and
HTML-like input are covered without exposing another case's content.

## 2. Extract searchable file content

Deliver this in three useful increments, building shared support alongside the
first extractor that needs it.

1. **Native document text and media metadata.** Start with an explicitly bounded
   UTF-8 text/PDF input set, then image metadata and audio technical metadata.
   Retain derived values separately from supplied values and originals. Add
   page/text anchors, private result access, processing versions, and rerun
   history. Add a worker and recoverable jobs with the first lengthy parser;
   bound bytes, pages, processing time, and decompression resources.
2. **Timestamped audio transcription.** Add the centralized Gateway integration,
   explicit external-processing disclosure, limits, accounting, failure states,
   and operator configuration. Support standalone audio and identified call
   attachments through the same processing path. Retain uncertain intervals;
   supplied transcripts remain separate retained sources.
3. **Printed-text OCR.** Support an evaluated image/scanned-page subset with
   source regions and reading order. Preserve uncertain characters. Broader
   layouts, formats, languages, and handwriting require their own evaluation
   before being advertised as supported.

Feed each completed extraction into case-scoped content search. Start with
PostgreSQL-backed lexical search and exact identifier lookup; add semantic
retrieval in priority 4 when there is useful extracted content to retrieve.
Add bounded email, phone, URL/domain, and explicit-date extraction with original
spans; keep ambiguous normalization and dates unresolved.

**Complete when:** an investigator can search a supported document, recording,
or image and inspect the original page, audio interval, or image region. Reruns
preserve history; unsupported, failed, partial, and empty extraction outcomes
are distinct. Revoked access prevents new processing/publication, and retries
do not duplicate completed results.

Native PDF text extraction does not handle scanned text by itself, and parser
resource consumption needs bounds; see [pypdf's extraction guidance](https://pypdf.readthedocs.io/en/stable/user/extract-text.html).
PostgreSQL supplies [text-search, ranking, and highlighting facilities](https://www.postgresql.org/docs/current/textsearch.html),
making it the starting choice within the existing stack. For inference, verify
the selected endpoint and its [Gateway authentication requirements](https://developers.cloudflare.com/ai-gateway/configuration/authentication/);
provider/model choice remains an implementation decision, not a claim of support.

## 3. Build investigation workflows

These are independent tracks. Share result/provenance handling from priority 2,
but do not require every media extractor before starting them.

### 3A. Conversations first by default

Use existing messages for an ordered conversation reader, explicitly inferred
sessions, timing/turn-taking statistics, and copied-phrase review. Add a bounded
source-specific importer, starting with the documented iMessage HTML profile
examined in the catalogue. Retain the original export and source anchors.

Preserve participant/account labels, message/thread IDs, replies, groups, and
attachments when a source actually supplies them, adding the required context
support deliberately. The iMessage HTML profile must not be assumed to contain
all of them. Keep uncertain person mapping reviewable and retry imports safely.
Native relationships and inferred sessions remain distinguishable.

Then add source-linked entities, plans, commitments, questions/answers, topic
continuation, and per-session summaries. Use the Gateway path for model-based
work. Whole-case questions and synthesis follow in priority 4.

**Complete when:** an investigator can move from a proposed session or extracted
commitment to its exact messages, see how grouping was inferred, and retain
unresolved participant/reference ambiguity. Import retries and partial failures
preserve source integrity without silently creating duplicate messages.

### 3B. Locations, drives, and frequently visited places

Build on priority 1's coverage checks. Start spatial work on retained coordinates;
for address-only vehicle imports, add reviewable address normalization and
geocoding with match granularity and source attribution. Disclose external lookup
use. Keep geocodes separate from observations and retain ambiguous matches.

Then deliver candidate stop/visit segmentation, frequent-place counts, dwell
bounds, and candidate trips. Show observations and gaps alongside the inferred
result. Begin with an accessible ordered list/table and supporting map; the map
must not turn gaps into asserted routes. Add recurring place pairs and simple
proximity checks before road-network route reconstruction or advanced co-location.

**Complete when:** repeated stationary samples yield defensible candidate visit
counts under visible assumptions, gaps remain visible, and every trip/visit
links to its observations. The feature never infers the driver, verifies a
person's presence, or silently labels a frequent place as home/work.

### 3C. Descriptive writing styles

Start on original Text and Text message evidence with known source context;
this track does not need OCR, transcription, or native conversation imports.
Deliver sample selection, quoted/copied-text exclusions, descriptive feature
profiles, and comparison over time/channel. Explain punctuation, word choice,
message length, repeated wording, and the amount of usable material.

**Complete when:** differences are backed by examples and sample coverage,
OCR/transcriber artifacts are excluded or labelled, and insufficient material
can produce no conclusion. Named-author attribution is outside this increment.

## 4. Connect evidence and synthesize findings

Start as soon as the relevant extracted inputs are ready; completion of all
three priority-3 tracks is not required.

1. **Connect explicit values and events.** Add reviewable identifier/entity
   candidates and a unified timeline. Distinguish recorded event time, extracted
   dates, planned events, reported events, and upload time. Start with explicit
   references before ambiguous semantic relationships. Add duplicate-source
   review so copied material does not masquerade as independent corroboration.
2. **Retrieve related passages.** Add case-scoped semantic retrieval alongside
   lexical search. Evaluate relevant and irrelevant hits, enforce access at
   query/result time, and preserve the source selection and index provenance.
3. **Synthesize selected evidence.** Deliver cited answers, supporting/conflicting
   passages, and selected-evidence case summaries. Retain unknowns, contrary
   evidence, missing periods, and unanswered questions. Connect conversations
   to candidate journeys only when both input tracks are available.

**Complete when:** every candidate link, timeline entry, or summary claim can be
inspected against sources; unsupported assertions can be rejected without
changing originals; and unavailable evidence cannot leak through search or
generated answers. A visually elaborate graph is not required for this milestone.

## 5. Add targeted specialist tools

Default order within this tier is plate extraction and image near-duplicates,
then structured document extraction, then evaluated face grouping/reference
comparison. Select supported conditions explicitly and preserve alternatives.

- **Plates and image comparison:** use existing image decoding, region anchors,
  OCR, and review controls. Retain ambiguous plate strings and image similarity
  reasons. Candidate sightings do not establish vehicle ownership or drivers.
- **Tables, forms, and transactions:** extend the supported document set after
  text/page provenance works. Preserve units, row/column meaning, subtotals,
  and document-type distinctions before aggregation.
- **Face grouping/reference comparison:** add quality screening, reference
  attribution, false-match evaluation, no-match outcomes, and review before
  release. Gateway compatibility for the selected inference path is a
  prerequisite, not something to bypass for convenience.
- **Demand-led additions:** translation for multilingual cases, speaker
  separation for overlapping/multi-speaker recordings, QR/barcode decoding,
  document classification, and object/scene labels. Bring translation or speaker
  separation into priority 2 when they are necessary to make its target inputs usable.

**Complete when:** the selected tool demonstrably reduces review work on
representative supported material while exposing errors, alternatives, and
source regions. Its acceptance criteria cover negative examples, not just
successful demonstrations.

## 6. Keep research-sensitive work conditional

This is an evaluated backlog, not a promised release bundle. Candidate-author
attribution, handwriting/signature comparison, and visual landmark inference
are **3/1** in the catalogue; manipulation triage is **2/1**, and new video is
**4/1**. Reference-voice comparison is **4/2**, but follows a dependable audio
pipeline and a demonstrated speaker-comparison need.

Advance one of these only when there is a concrete investigation use, suitable
reference/evaluation material, a compatible processing path, and an agreed
useful outcome that can abstain. Evaluate topic/channel confounding for
authorship and false matches for biometric comparison. Video additionally needs
retention, decoding, frame/time provenance, and operational limits; current
FaceTime records do not contain video.

Cross-case matching remains outside this roadmap's case-scoped assumptions.
Revisit advanced route inference and broad semantic linking only after their
basic observation and relationship workflows are dependable.

## Recommended first three implementation increments

1. **Search retained text and transcripts** in the existing Evidence collection.
   This is the first deliverable: useful immediately and independent of AI.
2. **Add call and vehicle-observation reports**, plus exact-file duplicate
   candidates, with source links and missing-value/coverage explanations.
3. **Deliver bounded native-document text extraction** with the minimum durable
   results, source anchors, background execution, and recovery it needs. Extend
   the search from increment 1 to those results.

Then add Gateway-backed transcription and local printed-text OCR, while starting the
conversation/location/style tracks as their dependencies become available.
Build the shared support with these features rather than scheduling a separate
open-ended platform project.

## Completion criteria for every release

Each release needs the original/provenance, uncertainty, permission, and failure
handling described in the [catalogue](evidence-analysis.md#shared-requirements-for-future-analysis).
Owners/editors initiate processing and review; viewers inspect authorized
results. Background work rechecks current authorization before execution,
external submission, retries, and publication. Already dispatched external
requests cannot be recalled; document the resulting cancellation/publication
behavior instead of implying retroactive revocation.

Validate with synthetic/sanitized fixtures and representative failure cases.
Follow [the existing validation workflow](validation.md): PostgreSQL-backed
checks, mocked inference failures, migration validation, and isolated Compose
smoke tests when infrastructure changes. Visible features also need the
desktop/mobile/keyboard review in [DESIGN.md](../DESIGN.md). Complete the required
simplicity review on each scoped implementation.

Evaluate useful task completion and inspectable correctness before expanding
scope: can investigators find the source, understand uncertainty, and recover
from failure? Set feature-specific quality and resource bounds during design;
do not invent accuracy percentages or delivery dates from the planning scores.
