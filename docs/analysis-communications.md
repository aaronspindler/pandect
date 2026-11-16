# Text, writing, and conversation analysis

These operations use the shared case-scoped analysis run, source, result, and
review workflow. Originals and supplied transcripts remain unchanged. Every
selection and parameter belongs to a retained run version. Review or rerun an
inferred result without changing a source-native relationship.

## Supported text operations

`entities` extracts explicit email, phone, HTTP(S) URL, bare domain, date,
currency-marked amount, patterned identifier, and coordinate strings. Results
retain raw strings and character spans. Phone country, ambiguous date order, currency-symbol
meaning, ownership, names, and free-form addresses remain unresolved. URL host
and email-domain case normalization preserves paths and email local parts. This
rule-based subset works without Gateway credentials. Bare domains require ASCII
labels of at most 63 characters, at most 253 characters overall, and a suffix in
the conservative allowlist in [`text/analysis/deterministic.py`](../text/analysis/deterministic.py).
This rejects common file extensions and dotted prose. Other suffixes and IDNs
remain unsupported; domain appearances inside an extracted email or URL are
already represented by that occurrence, rather than duplicated as bare domains.
No DNS lookup, availability test, URL navigation, or ownership inference occurs.
It accepts retained Text, Text messages, identified supplied call transcripts, and explicitly selected
completed extraction text. Source-result versions must remain identifiable.

`copied_text` compares at most 50 sources, 100,000 characters total, and 5,000
words per source. It reports the longest case-folded exact token run of at least
8 words by default; `minimum_words` may select 5–30. A whole-text token alignment
of at least 85% can also propose lightly edited wording when both sources have
at most 1,000 words. These are comparison rules, not calibrated probabilities.
Whitespace and punctuation are ignored for matching while offsets and displayed
passages use the original text. The first 200 matching source pairs are retained;
a reached limit is partial coverage. Shared wording can reflect quotation,
boilerplate, a repeated export, or source dependence and is not authorship proof.
Each result supplies candidate spans for investigator-reviewed style exclusions.

`text_topics` supports observed keywords with `include_summary: false` and
model-based source-cited topic suggestions and short summaries otherwise.
The keyword baseline uses English stop words, with a disclosed language limit.
`content_events` uses the same model-content path to propose attributed claims,
plans, commitments, cancellations, reported events, questions, and candidate
answers. Metadata-only calls have no content to extract.

Model content requests include only the selected text and require exact quoted
passages from selected sources. An absent quote, unknown source, bad offset, or
ambiguous repeated quotation is rejected. A valid quotation verifies the source
anchor, not the model's interpretation. Negation, sarcasm, hearsay, quotations,
relative dates, missing exchanges, conflicting statements, and proposed versus
completed actions remain review concerns. Each request is limited to 64,000
selected characters and 100 returned suggestions. A local runtime additionally
checks its complete token budget without truncation. Unavailable inference is a
recoverable limitation; missing text is insufficient evidence. Inference defaults
to the configured private runtime through the shared backend. External inference
uses Cloudflare AI Gateway only when separately selected and approved, with no
automatic fallback. See [local analysis](local-analysis.md) for setup and
per-method quality qualification.

## Descriptive writing profiles

`style_profiles` uses original Text and Text message samples. Derived OCR,
transcription, and translation formatting are excluded from writing habits.
Counts include punctuation, capitalization, vocabulary and function words,
selected spelling variants, message/sentence length, abbreviations, Unicode
symbols, repeated three-word phrases, greetings, and sign-offs. Exact examples
link to retained character offsets. Unicode-symbol counts include some symbols
that are not emoji and count code points rather than full emoji sequences.
Spelling variants are observed forms, not diagnoses of spelling errors.

Parameters preserve preparation decisions:

- `exclusions` maps selected source IDs to `[start, end]` character-span lists.
- `exclude_quotes` defaults to true for quoted lines and straight/curly paired
  double-quoted passages. These are disclosed quotation candidates.
- `contexts` maps selected source IDs to supplied context, including `language`,
  `origin`, channel, topic, audience, and attribution. An origin other than
  `original` excludes that sample from writer-formatting measurements.

Language is supplied context; this baseline does not automatically identify
languages or locate code-switching boundaries. Mixed labels and unknown language
remain visible. Attribution may refer to shared accounts. Topic, genre, channel,
audience, autocorrect, dictation, editing, translation, OCR, and transcription can
change observed features without a change of writer.

The initial display guard needs at least 30 usable words, 12 distinct words, and
a vocabulary ratio of at least 0.1 for a useful descriptive profile. These are
explicit conservative product rules, not validated attribution thresholds.
Short, repetitive, or heavily excluded selections yield **insufficient evidence**;
raw counts remain inspectable without a conclusion. Profiles do not infer
identity, personality, protected traits, truthfulness, intent, or mental state.
See the primary research on [style versus topic/genre effects](https://aclanthology.org/C18-1238/).

`style_change` compares two disjoint lists of selected source IDs, `group_a` and
`group_b`. It reuses the prepared usable text, counts, and exclusions, exposes
sample coverage, and compares each feature with exact usable-text examples.
Per-source feature measurements provide supporting, contrary, or mixed/shared
examples relative to group aggregates. An absence of contrary examples is not
proof of a uniform writer. Example spans never cross excluded passages.
Recurring three-word wording is compared between groups with counts and exact
original spans; excluded gaps never create artificial word sequences.
Insufficient groups do not produce change conclusions. Unequal sample sizes, different periods,
recipients, channels, languages, and topics remain confounders; no author ranking
or new-author conclusion is produced.

Each group reports available and missing reported message-event times and its
selected channel context. Upload and collection times never become writing
times. A possible observed-style comparison interval appears only when both
groups are adequate, every usable sample has a reported event time, the periods
do not overlap, and their measured features differ. Its bounds are the last
selected earlier-period message and first later-period message. It does not
locate an actual change point, fill a missing period, or establish a new writer.
Unknown or overlapping periods produce no interval.

## Conversation sessions and interaction

The [conversation reader](evidence.md#reading-conversations) in **Case Tools →
Conversations** reads retained messages directly, across imported and individually
entered records, with an optional retained-export filter and inline message
details that open without a reload. It does not require an analysis run or use
inferred session boundaries. The operations below instead analyse their exact
retained selections; their output may cover only part of a participant conversation.

**Read inferred message sessions**, **Inspect message timing**, and **Summarize
message sessions** accept Text message evidence. Phone calls, FaceTime
records, and their transcripts are not message-session inputs. To summarize or
inspect claims in supplied call transcripts or retained transcriptions, select
their exact retained sources in **Inspect keywords and topics** or **Suggest
claims and commitments**. Cross-channel comparison below also accepts calls.

`sessions` orders selected messages by reported event time, breaking equal-time
ties by retained message/source IDs. Missing-time messages form unresolved
singletons and never bridge timed exchanges. Source-native participant context,
when present, remains scoped to its retained export. Otherwise same-case reported
participant pairs determine a comparison stream; these links do not verify
identity. The investigator form starts with a 60-minute gap boundary and accepts
1–10,080 minutes. If `gap_minutes` is omitted, including when the optional field
is cleared, the processor uses 120 minutes. A greeting after a farewell and at
least five observed minutes is a disclosed content boundary, enabled by default;
the internal `greeting_boundaries` parameter is not an investigator form control.

The session reader retains original messages, order, boundary reasons, reported
participant associations, native message/thread/reply references when supplied,
and observed first/last timestamps. An inferred session number is independent
of native thread identity. `break_before` and `join_before` record adjustments
using selected source IDs in a new retained run version. Missing exchanges and
simultaneous topics can split or combine discussions incorrectly.

`comm_patterns` measures selected message counts, reported UTC hours, observed
turn changes, apparent session initiations, observed session spans, and response
intervals using the session reader's exact grouping, greeting/farewell cues, and
investigator `break_before`/`join_before` adjustments. Each session's source order,
boundary reason, and measurements remain inspectable. An interval is elapsed
recorded time between consecutive selected messages from different senders within a participant group/time window. It does
not prove a response to that message, reading time, deliberate nonresponse,
relationship quality, motive, or emotion. Self-addressed records do not show a
response by another participant. `compare_split_at` accepts an explicit ISO
timestamp with timezone and displays both groups' denominators and coverage.
A period beginning midway through a session does not create another apparent
initiation; it reports a fragment of the same disclosed session. Unknown-time
sessions have unknown spans. Missing export periods are unknown coverage.

`conversation_link` proposes adjacent exchanges across messages, Phone calls,
and FaceTime using recorded participant/time context. The investigator form starts
with a 10-minute comparison window and accepts 0–1,440 minutes, saved as
`time_window_minutes`. If no window is supplied, the processor uses 180 minutes;
its internal `window_minutes` parameter supports 0–10,080 minutes. Zero compares
only equal recorded times. Content wording overlap is labelled as a separate
weak cue. Metadata-only calls supply ordering, not invented speech. Optional
`semantic_links` uses model-based two-sided quotation citations to propose
continued topics, events, and earlier references. A completed semantic candidate
needs exact citations to at least two distinct selected sources. One-sided or repeated citations to one source retain an
unresolved suggestion and a partial result rather than becoming a completed
link. Temporal adjacency and shared topics do not prove the same event or
causation; “that place” can retain several possible referents. Temporal output is
limited to 250 candidates with omitted counts and partial coverage disclosed.

`session_summaries` sends only selected Text messages in inferred-session chunks
to the shared content path: at most 16,000 characters per chunk and 20 chunks per run.
Every summary states its chunk/source selection. A missing or oversized chunk
must not silently become an apparently complete history. These concepts adapt
[iMessageLLM's documented workflow](https://github.com/aaronspindler/iMessageLLM)
without its two-person reduction or bypassing Pandect's shared inference boundary.

## Retained iMessage HTML import

The supported `imessage-html-v1` profile begins with the specific
[inspected parser structure](https://github.com/aaronspindler/iMessageLLM/blob/14a46b9826ad2d8a0c0777d3cfd4394a49f3ce9a/process.py):
`div.message` with exactly one `span.timestamp`, `span.sender`, and `span.bubble`.
This does not claim general compatibility with all imessage-exporter releases.
Select one retained UTF-8 HTML Document, at most 2 MiB and 500 messages. Parsing
bounds nesting (40 levels) and nodes (20,000), never executes scripts, and never
fetches links or attachment paths. The original HTML is retained; extracted
message bodies are decoded visible text, not original Messages database bytes.

Choose an explicit IANA `timezone`. ISO timestamps with offsets retain their
interpretation; the inspected `%b %d, %Y  %I:%M:%S %p` display format requires the
selected zone. Ambiguous or nonexistent daylight-saving times, timestamps over
255 characters, and malformed rows remain unresolved. Missing times remain
unknown.

The investigator form supports **1–100 source account labels**, initially `Me`
and `Them`. Add or remove mapping rows without JavaScript; these actions preserve
the selected document and entered values without queuing analysis. Enter each
exact label once and choose an existing same-case person. Each label may also
have an explicit **Recipient when the source omits one**. Leaving that selection
blank keeps recipient-less rows unresolved; another mapped person is never
automatically chosen.

The form creates the retained `sender_map` and explicit per-sender
`recipient_map`. An optional `span.recipient` uses its own label's person mapping;
unmapped explicit recipients remain unresolved even when a fallback is selected.
No person is guessed from a label, display name, or timing. Each imported message
still has one sender and one recipient. Group labels retain full source context
without asserting that those are the group's only members. Existing parser
versions and retained interpretation/idempotency checks are unchanged.

The tested optional context extension preserves only what the source supplies:

| Source field | Retained context |
| --- | --- |
| `data-message-id`, `data-thread-id`, `data-reply-to` | Native message/thread/reply references |
| `data-participants` JSON label array | Source group participant labels |
| `data-account`, `data-service` | Raw account/service identifiers |
| `data-reaction`, `data-edited`, `data-deleted`, `data-forwarded`, `data-group-id` | Source context markers |
| Elements with class `attachment` | Original href/src/label reference; bytes remain un-ingested |

Missing fields stay unknown. An edit/deletion marker does not recover earlier
or deleted content. Attachment references are not private file access or verified
availability. No arbitrary path is opened and no public storage URL is created.

**Start analysis** queues parsing and publication with the selected mapping and
timezone. There is no separate row-preview confirmation step as in vehicle CSV
imports. Preparation writes no messages. At current-authorized atomic publication,
`apply_import` creates each mapped message and immutable `CommunicationContext`
with the retained document, checksum, parser version, timezone, row and exact
HTML character range. Unresolved rows stay visible in the partial result for
recovery. Whole publication rollback removes newly created messages/context.
Retries reuse the same document/row identity; a conflicting mapping or content
interpretation is rejected for review instead of replacing the earlier original.
Each mapped body is stored once in the retained result as the finding text used
for publication. The shared analysis result limit still applies to the complete
result.

Imported messages do not individually queue automatic local checks. A completed
import offers **Run local checks on imported evidence**, with its eligible count
and a link to the resulting Processing batch. Atomic publication records each
row's exact message ID and whether it was newly created or already retained.
Only newly created rows enter that import's checks; unresolved and skipped
duplicate rows do not. Independent entity and keyword checks use bounded groups;
session, relationship and movement analysis remain deliberate whole-population
choices. Repeated submission of the same request does not enqueue another batch.

Historical results without publication membership retain an explanatory message
instead of guessing which rows were new. Their messages remain available for
individual analysis or case **Rerun all analysis**.

Import method version `3` uses parser `imessage-html-v3` with the same supported
HTML profile. It handles valueless optional attributes, flags oversized timestamps
for review, and stores mapped message text once. It preserves version 2's exact
source ranges based on literal LF line positions and closing tags. Existing results
and imported messages retain their recorded provenance. A new run reuses previous
messages when their interpretation agrees.

Finish queued imports with the old worker before upgrading when possible. Queued
method `1` or `2` runs otherwise report that their method version is unavailable.
Start a new run with the same source and settings. The upgrade requires no data
migration.

## Message CSV imports

Choose **Add evidence → Import records → Import iMessage · CSV**, or use the same
action in a retained Document's **More** menu. Upload one CSV or choose a retained
Document and explicitly select its export timezone. The supported profile requires
these 18 column headers, in any order:

`Chat Session`, `Message ID`, `Message Date`, `Delivered Date`, `Read Date`,
`Edited Date`, `Deleted Date`, `Service`, `Type`, `Sender ID`, `Sender Name`,
`Status`, `Replying to`, `Subject`, `Text`, `Reactions`, `Attachment`, and
`Attachment type`.

UTF-8 with an optional BOM, quoted commas, and quoted multiline text are supported.
Other CSV layouts are unsupported. There is no
message-row limit. The normal configured upload limit applies (100 MiB by
default), with 131,072 characters per field and the configured message-text byte
limit. CSV preparation, review, and publication read rows incrementally rather
than collecting the entire conversation in an analysis result.

Preparation runs in the background and creates no people or messages. The review
shows the source contact labels, message counts, reported times, text, and excluded
rows with reasons. Select existing same-case people or explicitly create missing
people for **Export account** and **Other participant**. Source names and numbers
are suggestions, not verified identities or automatic matches; names are not
automatically split. A person may be identified by a name, phone number, or email.
The account's phone number is not required when selecting an existing person.

Confirm that this is one direct conversation. Multiple chat sessions or incoming
sender identifiers are rejected; group and mixed-chat imports are unsupported.
The displayed chat label alone cannot establish who participated. Outgoing
messages use the export account as sender and the other person as recipient;
incoming messages reverse those associations. Changing the timezone prepares a
new review. Missing event times remain unknown; malformed, ambiguous, or
nonexistent local event times are excluded. Attachment-only rows and unsupported
directions are also excluded. Confirming imports the valid text rows while
retaining excluded material in the CSV and preview.

Confirmation queues publication using import method `2`. The preview and
confirmed import are separate immutable analysis runs. Publication rechecks
current editing access and source integrity, then creates requested people,
messages, immutable source contexts, and the result atomically. Repeated
confirmation and worker retries do not create another set of people or messages.
An already imported source row with a different interpretation is a conflict;
earlier originals are never replaced. As with HTML imports, document/row identity
does not deduplicate distinct retained exports using native message IDs.

Method `2` retains a compact summary and a digest of the reviewed row
interpretation. Review pages read 25 rows at a time from the checksum-verified
original using the retained timezone and text limit; the first three rows supply
visible excerpts. Changes to row eligibility or time interpretation require a
new review. The general analysis-result item and byte ceilings do not truncate
imports because messages are not stored as individual result items. Publication
uses bounded groups inside one transaction and rolls back the whole import on a
conflict or failure. Completed message links are paginated too.

Newly created message contexts retain their publishing run through the nullable
`CommunicationContext.import_run` link. Apply its additive migration before
starting the updated web and worker processes. Existing imported messages and
source contexts are not rewritten. Historical method `1` results and local-check
membership remain readable; prepare a new review from the retained CSV before
confirming an old preview. Queued method `1` runs require a new method `2` run
after upgrading.

Each message preserves the decoded **Text** without trimming and links to its
source row and exact character range. The original CSV retains its exact bytes.
**Message ID** and **Replying to** remain source-native references. **Chat Session**
remains a label, not an asserted native thread ID. **Info → Imported source
context** exposes service, direction, source contact details, subject, status,
delivery/read/edit/delete dates, reactions, and attachment references. These are
source claims; reactions do not become separate messages and edit/deletion
markers do not recover missing history. Attachment paths are never opened or
fetched, and referenced files are not ingested.

Confirmed CSV imports automatically queue analysis for exactly the messages newly
created by that import. The preparation task commits with publication; a preview,
excluded row, or reused message does not start another analysis batch. The import
page links to **Follow message analysis** in Processing. **Run message analysis
again** prepares a new batch while preserving earlier results. Historical imports
are not backfilled automatically. HTML imports retain their deliberate local-check
action.

The automatic suite includes identifiers/dates, keywords, inferred message
sessions, timing, writing profiles, repeated wording, temporal exchange links,
local topic suggestions, claims/commitments, session summaries, and semantic
exchange suggestions. Writing profiles use separate reported senders. Writing
comparisons use that sender's earlier/later timed samples, split near the middle
at a distinct timestamp without dividing equal-time messages; fewer than two
distinct times cannot produce a comparison. Existing adequacy guards still report
insufficient evidence rather than attributing authorship.

Preparation walks all newly imported messages in reported-time order, with unknown
times last, using at most 250 messages (or the configured lower source limit) and
one million text characters per step. It queues repeated-wording comparisons in
groups of at most 50 messages/100,000 characters, and model work in groups of at
most 20 messages/16,000 characters. An individual oversized message is retained
whole; an unsupported analysis fails visibly rather than truncating it. Each
result retains its exact selection and counts against the import population.
Smaller selections disclose partial coverage: session boundaries, timing, writing,
and wording comparisons do not span separate selections. These are bounded views,
not whole-conversation or whole-case conclusions.

Temporal exchange comparisons also select retained, unarchived same-case Phone
call and FaceTime metadata for the reported participant pair within ten minutes
of the message window, up to the remaining analysis-source capacity. Other calls,
transcripts, and material outside the selection remain unexamined. Semantic
suggestions cover the selected imported messages only.

Automatic model work uses the shared local/private runtime, with pinned execution
provenance and exact quotation validation. Missing configuration, runtime drift,
capacity limits, and provider/output failures remain visible, retryable runs;
they do not undo messages or prevent independent rule-based results. There is no
automatic external AI selection or cloud fallback. Case editing access and
verified requester status are rechecked at preparation and execution. Preparation
commits its cursor, runs, and continuation together; retries do not duplicate
queued work. A stopped preparation can be rerun from the import page after the
old worker has stopped; a failed individual run uses normal Processing recovery.
