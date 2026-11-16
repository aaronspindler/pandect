# Text, writing, and conversation analysis

These methods use shared case-scoped runs, selections, parameters, results and
reviews. Originals/transcripts stay unchanged. Rerunning/reviewing inferred output
never changes source-native relationships.

## Supported text operations

`entities` retains explicit email, phone, HTTP(S) URL, bare domain, date,
currency-marked amount, patterned identifier and coordinate strings with original
spans. Phone country, ambiguous dates/currency symbols, ownership, names and free
addresses remain unresolved. Host/email-domain normalization preserves URL paths
and email local parts. No Gateway credentials are needed.

Bare domains allow ASCII labels up to 63 characters, 253 overall, and the
[conservative suffix allowlist](../text/analysis/deterministic.py), excluding file
extensions/dotted prose. Other suffixes and IDNs are unsupported. Domains inside
emails/URLs are not duplicated. No DNS/availability/ownership lookup or navigation
occurs. Inputs are retained Text/messages, identified call transcripts and selected
completed extractions, with identifiable result versions.

`copied_text` allows 50 sources, 100,000 total characters and 5,000 words/source.
It reports longest case-folded exact token runs, defaulting to eight words
(`minimum_words`: 5–30). Whole-text alignment of at least 85% can suggest lightly
edited wording when both sources have 1,000 words or fewer. Matching ignores
punctuation/whitespace; displayed spans preserve originals. The first 200 matched
pairs are retained, with reached limits marked partial. These uncalibrated rules
establish no authorship: quotation, boilerplate, copied exports and dependence
remain possible. Candidate spans support reviewed style exclusions.

`text_topics` supplies observed keywords with `include_summary: false`; otherwise
it adds cited model topics/short summaries. English stop words impose a disclosed
language limit. `content_events` proposes attributed claims, plans, commitments,
cancellations, events, questions and candidate answers through the same content
path. Metadata-only calls provide no speech.

Model requests send only selected text and require exact source quotations.
Absent/unknown/ambiguous repeated quotes or invalid offsets are rejected. Valid
anchors do not establish interpretation accuracy: negation, sarcasm, hearsay, quotation, relative
dates, missing exchanges, conflicts and planned/completed actions need review.
Requests allow 64,000 characters and 100 suggestions; local runtime token budgets
also apply without truncation. Missing text is insufficient; unavailable inference
fails recoverably. Private inference defaults; separately selected/approved
Cloudflare Gateway has no automatic fallback. See [setup/qualification](local-analysis.md).

## Descriptive writing profiles

`style_profiles` uses original Text/messages, excluding derived OCR/transcription/
translation formatting. It counts punctuation, capitals, vocabulary/function words,
spelling variants, message/sentence lengths, abbreviations, Unicode symbols,
three-word phrases, greetings and sign-offs, with exact spans. Symbol counts use
code points, not full emoji sequences, and include non-emoji. Spelling variants
are observations, not error diagnoses.

Preparation parameters retain:

- `exclusions`: source IDs mapped to `[start, end]` character spans.
- `exclude_quotes`: default true for quoted lines and paired straight/curly double
  quotes; these remain disclosed candidates.
- `contexts`: source language, `origin`, channel, topic, audience and attribution.
  Non-`original` origins exclude writer-formatting measurements.

Language is supplied, never automatically identified or split at code switches.
Mixed/unknown labels remain visible; attribution can describe shared accounts.
Topic, genre, channel, audience, autocorrect, dictation, editing, translation,
OCR and transcription can change features without changing writer.

Useful profiles require at least 30 usable words, 12 distinct words and a vocabulary
ratio of 0.1 or more. These conservative display rules are not validated attribution thresholds.
Short/repetitive/heavily excluded selections retain counts with **insufficient
evidence**, without conclusions. Profiles infer no identity, personality, protected
traits, truthfulness, intent or mental state; see
[style/topic/genre research](https://aclanthology.org/C18-1238/).

`style_change` compares disjoint `group_a`/`group_b` source lists, reusing prepared
text/counts/exclusions and exposing coverage. Feature examples show supporting,
contrary or mixed/shared evidence against group aggregates. An absence of contrary
examples does not prove a uniform writer. Spans never cross exclusions. Three-word comparisons retain
counts/exact spans without artificial sequences across gaps. Insufficient groups
produce no conclusions. Size, period, recipient, channel, language and topic
confound comparison; no author ranking/new-writer conclusion is produced.

Groups disclose channel context and present/missing reported message times;
upload/collection times never become writing times. An observed-style interval
requires adequate groups, timed usable samples, disjoint periods and differing
features. It spans the last earlier-period message to first later-period message,
without locating an actual change, filling gaps or establishing a new writer.
Unknown/overlapping periods produce no interval.

## Conversation sessions and interaction

**Case Tools → Conversations** [reads retained messages](evidence.md#reading-conversations)
from imports/individual entries, optionally filtered by export, with inline details
without reload. No analysis/session inference is needed. The methods below use
exact selections, potentially covering only part of a conversation.

**Read inferred message sessions**, **Inspect message timing** and **Summarize
message sessions** accept Text messages, excluding calls/FaceTime/transcripts.
Select exact call transcripts/transcriptions under **Inspect keywords and topics**
or **Suggest claims and commitments**; cross-channel comparison also accepts calls.

`sessions` orders reported times, breaking ties by message/source IDs. Untimed
messages form unresolved singletons and never bridge exchanges. Source-native
participant context stays scoped to its export; otherwise same-case reported
pairs define streams without verifying identity. Forms default to 60-minute gaps,
range 1–10,080; omitted/cleared `gap_minutes` uses the processor's 120 minutes.
Greeting after farewell plus five observed minutes enables a disclosed boundary
by default; internal `greeting_boundaries` has no form control.

Readers retain messages/order, boundary reasons, participant associations,
supplied native message/thread/reply references and first/last times. Inferred
session numbers are independent of native threads. `break_before`/`join_before`
adjust selected source IDs in new run versions. Missing exchanges/simultaneous
topics can cause incorrect boundaries.

`comm_patterns` measures counts, UTC hours, turn changes, apparent initiations,
spans and response intervals using those exact groups/cues/adjustments. Each
session's order, reasons and measurements remain inspectable. Intervals measure
consecutive different-sender selected messages within a participant/time group,
not verified replies, reading, deliberate nonresponse, relationship quality,
motive or emotion. Self-addressing shows no other participant's response.
`compare_split_at` requires an explicitly zoned ISO timestamp; groups show
denominators/coverage. Mid-session periods retain fragments, not new initiations.
Untimed spans/export gaps remain unknown.

`conversation_link` proposes adjacent message/call/FaceTime exchanges using
participant/time context. Forms default to ten minutes, range 0–1,440, saved as
`time_window_minutes`; omission uses 180 minutes. Internal `window_minutes`
allows 0–10,080. Zero compares equal times only. Wording overlap is a separate weak
cue; metadata invents no speech. Optional `semantic_links` uses two-sided model
quotations for topics/events/earlier references. Completed links require citations
to at least two distinct selected sources; one-sided/repeated-source citations remain
unresolved/partial. Adjacency/topics prove neither shared events nor causation;
ambiguous references retain alternatives. Temporal output caps at 250 candidates,
with omissions/partial coverage disclosed.

`session_summaries` sends selected messages through shared content processing in
inferred-session chunks, up to 16,000 characters/chunk and 20 chunks/run. Each
summary discloses chunk/selection; oversized/missing chunks never imply complete
history. This adapts [iMessageLLM](https://github.com/aaronspindler/iMessageLLM)
without two-person reduction or inference-boundary bypass.

## Retained iMessage HTML import

`imessage-html-v1` supports the [inspected structure](https://github.com/aaronspindler/iMessageLLM/blob/14a46b9826ad2d8a0c0777d3cfd4394a49f3ce9a/process.py):
`div.message` with exactly one `span.timestamp`, `span.sender` and `span.bubble`,
without claiming all imessage-exporter versions. Select one retained UTF-8 HTML
Document up to 2 MiB/500 messages. Parsing limits nesting to 40 and nodes to 20,000,
executes no scripts and fetches no links/attachments. Bodies are decoded visible
text; original HTML is retained, not original Messages database bytes.

Choose IANA `timezone`. Offset ISO timestamps keep their interpretation;
`%b %d, %Y  %I:%M:%S %p` needs the selected zone. Ambiguous/nonexistent DST times,
malformed rows and timestamps above 255 characters remain unresolved; missing
times stay unknown.

Forms accept **1–100 source account labels**, initially `Me`/`Them`. Mapping-row
changes work without JavaScript, retaining document/values without queuing work.
Enter exact labels once and select same-case people. Explicit **Recipient when
the source omits one** is optional; blank leaves unresolved rows, never an inferred
other person.

Forms retain `sender_map`/`recipient_map`. Optional `span.recipient` uses its own
mapped label; unmapped recipients stay unresolved despite fallbacks. No identity
is guessed from labels/names/timing. Messages retain one sender/recipient;
group labels preserve context without asserting exclusive membership. Retained
interpretation/idempotency checks and parser-version provenance remain intact.

Optional context retains only supplied fields:

| Source field | Retained context |
| --- | --- |
| `data-message-id`, `data-thread-id`, `data-reply-to` | Native message/thread/reply references |
| `data-participants` JSON label array | Source group participant labels |
| `data-account`, `data-service` | Raw account/service identifiers |
| `data-reaction`, `data-edited`, `data-deleted`, `data-forwarded`, `data-group-id` | Source context markers |
| Elements with class `attachment` | Original href/src/label reference; bytes remain un-ingested |

Missing fields remain unknown. Edit/deletion markers recover no history;
attachment references grant neither access nor verified availability. Paths are
never opened and no public URL is created.

**Start analysis** queues parsing/publication using selected mappings/timezone,
without a separate vehicle-style row confirmation. Preparation creates no messages.
Authorized atomic `apply_import` creates mapped messages and immutable
`CommunicationContext` with document/checksum, parser, timezone, row and exact
HTML range. Unresolved rows remain in partial results. Rollback removes new
messages/context; retries reuse document/row identity. Conflicting interpretations
require review, never replace originals. Each body appears once as finding text
within the shared total-result limit.

Imports queue no checks per message. Completed imports offer **Run local checks
on imported evidence**, eligible counts and Processing links. Publication records
exact message IDs and new/reused status; only new rows receive import checks.
Unresolved/skipped duplicates are excluded. Entity/keyword groups stay bounded;
session, relationship and movement analysis remain deliberate population choices.
Resubmission creates no duplicate batch. Historical results lacking membership
explain the limit instead of guessing; individual analysis/case reruns remain usable.

Method 3/parser `imessage-html-v3` preserves the profile, handles valueless optional
attributes, flags oversized times and stores bodies once. Version 2's LF/closing-tag
exact ranges remain. Existing provenance stays unchanged; agreeing reruns reuse
messages. Finish queued old imports before upgrading where possible. Queued methods
1/2 otherwise report unavailable versions; start another run with the same source/
settings. No data migration is needed.

## Message CSV imports

CSV uses `imessage_import`, parser `imessage-csv-v1` and result key `imessage`,
within the fresh pre-release database/queue baseline. Earlier identifiers have no
aliases; prior serialized CSV runs/contexts are unsupported and runtime imports
are not migrated. Source files/historical provenance remain unchanged.

Choose **Add evidence → Import records → Import iMessage · CSV** or a retained
Document's **More** action. Upload/select one CSV and explicitly choose export
timezone. These 18 headers are required in any order:

`Chat Session`, `Message ID`, `Message Date`, `Delivered Date`, `Read Date`,
`Edited Date`, `Deleted Date`, `Service`, `Type`, `Sender ID`, `Sender Name`,
`Status`, `Replying to`, `Subject`, `Text`, `Reactions`, `Attachment`, and
`Attachment type`.

UTF-8/optional BOM, quoted commas and multiline text are supported; other layouts
are not. No message-row ceiling applies. Message CSV files allow 100 MiB, separately
from ordinary uploads. Fields allow 131,072 characters and each message allows
at most 1 MiB UTF-8. Preparation,
review and publication stream rows rather than accumulating conversations in results.

Background preparation creates no people/messages. Review shows contact labels,
counts, times, text and excluded-row reasons. Select same-case people or explicitly
create **Export account**/**Other participant**. Names/numbers are unverified
suggestions, never automatic matches or split names. New person records can use a
name, phone number or email; selecting an existing person needs no account phone number.

Confirm one direct conversation. Multiple chats/incoming sender IDs are rejected;
group/mixed imports are unsupported. Chat labels alone identify no participants.
Outgoing uses account→other; incoming reverses them. Timezone changes prepare
fresh review. Missing times stay unknown; malformed/ambiguous/nonexistent local
times, attachment-only rows and unsupported directions are excluded. Confirmation
imports valid text while retaining exclusions in CSV/preview.

Confirmation queues method 2, separate from the immutable preview run. Publication
rechecks edit access/integrity and atomically creates requested people, messages,
contexts and results. Repeated confirmation/retries create no duplicates. Conflicting
source-row interpretations preserve originals. Document/row identity, as in HTML,
does not deduplicate distinct exports by native IDs.

Method 2 retains compact summaries/digests of reviewed interpretation and excerpts
from up to three initial rows. Ordinary review reads that retained summary;
automatic updates read only authorized run progress and offer a link when review
is ready. **Review all message rows** deliberately reads the checksum-verified
original and shows 25 rows using retained timezone/text limits. Confirmation also
rechecks the full source and interpretation before queueing publication. These
full-file checks share a limit of 10 per minute per user and 30 per minute per
case across web processes; excess requests return 429 before reading the source.
Summary pages and progress remain available. Eligibility/time changes need
fresh review. Result item/byte ceilings never truncate imports: messages are not
individual result items. Bounded publication groups share one transaction and
roll back entirely on conflict/failure. Completed message links paginate.

Nullable `CommunicationContext.import_run` retains publishing runs; apply its
additive migration before updated web/workers. Existing messages/contexts stay
unchanged. Within `imessage_import`, historical method-1 results/local-check
membership remain readable; old previews need fresh reviews, and queued method 1
needs a method-2 run.

Decoded **Text** is preserved untrimmed with source row/exact character range;
CSV bytes stay exact. **Message ID**/**Replying to** retain native references;
**Chat Session** stays a label, not a native thread ID. **Info → Imported source
context** shows service/direction, contact claims, subject/status, delivery/read/
edit/delete dates, reactions and attachment references. Reactions create no
messages, markers recover no history and attachments are never opened/fetched.

Publication atomically queues analysis only for newly created messages. Previews,
excluded/reused rows start no batch. **Follow message analysis** opens Processing;
**Run message analysis again** creates a new batch preserving history. Historical
imports receive no automatic backfill; HTML retains its deliberate action.

The suite covers identifiers/dates, keywords, sessions/timing, writing profiles,
repeated wording, temporal exchanges, local topics/claims/session summaries and
semantic exchanges. Writing profiles separate reported senders. Comparisons split
each sender's earlier/later timed samples near the middle at a distinct timestamp,
never dividing ties. Fewer than two times cannot compare; adequacy guards yield
insufficient evidence, never authorship attribution.

Preparation walks reported-time order, unknowns last, in steps of 250 messages or
the configured lower source limit, within one million characters. Repeated wording
uses 50-message/100,000-character groups; models use 20-message/16,000-character
groups. Oversized messages remain whole; unsupported analyses fail visibly without
truncation. Results retain exact selections/counts against import population.
Smaller selections disclose partial coverage; sessions, timing, writing and
wording do not span groups or imply whole-conversation/case conclusions.

Temporal exchanges additionally select unarchived same-case call/FaceTime metadata
for reported participant pairs within ten minutes of message windows, up to
remaining source capacity. Other calls, transcripts and unselected material stay
unexamined. Semantic suggestions cover imported messages only.

Automatic models use pinned private execution/quotation validation. Missing
configuration, drift, capacity and provider/output errors remain visible/retryable,
preserving messages and independent checks. No automatic external selection/cloud
fallback occurs. Preparation/execution recheck verified editing access. Cursor,
runs and continuation commit together; retries queue no duplicates. After confirming
the old worker stopped, rerun failed preparation from the import page; individual
failures use normal Processing recovery.
