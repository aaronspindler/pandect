# Text, writing, and conversation analysis

These methods use shared case-scoped runs, selections, parameters, results and
reviews. Originals/transcripts stay unchanged. Rerunning/reviewing inferred output
never changes source-native relationships.

## Supported text operations

`entities` retains explicit email, phone, HTTP(S) URL, bare domain, date,
currency-marked amount, patterned identifier and coordinate strings with original
spans. Phone country, ambiguous dates/currency symbols, ownership, names and free
addresses remain unresolved. Host/email-domain normalization preserves URL paths
and email local parts. Version 2 also identifies the literal relative dates
“today”, “yesterday” and “tomorrow” with exact offsets. Their reference date
remains unresolved in [Chronology](chronology.md). No AI credentials are needed.

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
cancellations, reported events, corrections, and candidate question/answer
pairs through the same content path. Keyword and topic output is a saved report.
Metadata-only calls provide no speech.

The shared cited model requests send the text, ID and type of each input, with a speaker
attribution label. Message inputs also include reported sender and recipient
UUIDs and the recorded event time when present. Pandect resolves display names
locally. Automatic requests can include bounded relevant case history as
described under [message import analysis](#message-csv-imports).

Suggestions require exact source quotations.
An exact quote that appears once in its cited source can be located without a
model-supplied offset. An ambiguous repeated quote needs the correct offset.
Review negation, sarcasm, hearsay, quotation, relative dates, missing exchanges,
conflicts and planned/completed actions in the originals.
Topic and claim requests allow up to 128,000 selected characters and 100
suggestions. Runtime context and request limits also apply without truncation.
Missing text is insufficient. Private inference is the default. Review and
approve the configured destination to use [external AI](analysis.md#external-ai-configuration).

## Compare a claim

Open **Case Tools → Analysis methods → Compare a claim**.
Choose the message or call transcript with the statement and one to five other
active text sources. Comparison evidence can include retained text, messages,
supplied call transcripts, or exact versions of saved text extractions and
transcripts. Select the claim source and copy its exact wording. Include enough
context to identify one occurrence if the wording repeats. The comparison
accepts up to 12,000 characters across the selection and up to 1,000 characters
for the claim. Runtime limits can require a smaller selection.

**Back to evidence selection** restores the selected sources, title search, and
source page. If that page is unavailable, the picker opens the first page with
the same title search and available selections. The picker reports any deleted
selections it clears. From an Evidence record that needs an extraction,
**Choose from all case evidence** broadens the search while keeping the
original return link. **Cancel** and the result's **Back to evidence** link
retain the starting record and filters. If that record was removed, they open
the filtered Evidence collection.

The queued comparison uses the configured [private text service](local-analysis.md).
The result retains the original claim, source-linked quotations, and potentially
supporting, potentially conflicting, or unresolved suggestions. Matching
checksums flag exact copies. Every selected source remains available for review.
Check attribution, dates, negation, cancellation, and contradictory or missing
context in the originals. A matching checksum establishes common bytes. A
different checksum leaves source independence unresolved.

Owners and editors queue comparisons. Current case viewers can read saved results
and their available sources. Archived or unavailable inputs stop processing and
withhold the result until the exact selection is available again. Processing
keeps the selected input versions, execution settings, retry, and download.

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

Shared cited model processing supplies the model with the same limits enforced
on its response: at most 100 suggestions, 1–10 citations per suggestion, and
4,000 characters per suggestion or quotation. Citation source IDs must come from
the selected inputs. Quotations must copy contiguous source text exactly,
including punctuation, whitespace and Unicode characters. For topics and claims,
Pandect can correct an offset when the quotation occurs exactly once in its
cited source. If a suggestion has an invalid citation, the entire suggestion is
omitted. When other suggestions can be verified, the result is marked partial
and counts the omissions. If all are rejected, claim analysis fails and topic
analysis can still retain its independently calculated keywords. Links and
session summaries stop on any invalid citation. Cross-channel links allow up to
64,000 characters. Session summaries use the smaller chunks described below.
[Safe worker diagnostics](background-tasks.md#failure-inspection-and-recovery)
identify the rejected category without retaining source text or model responses.

The [conversation reader](evidence.md#reading-conversations) uses retained
session memberships and displays a saved summary when the report covers the
entire grouping. Grouping uses active messages for the reported pair. A
conversation over the source limit or one million characters needs a smaller
manual selection.

Bulk review queues one summary per eligible session on the review page, using
the [case's selected AI backend](cases.md#ai-backend). It preserves saved
summaries and skips pending summaries with the same messages and grouping
settings, as well as sessions over the per-run source or chunk limit. Distinct
overlapping groupings can each have a summary.

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
cue, retained in the run. Optional `semantic_links` uses two-sided model
quotations for specific references, reported events, corrections, and
question/answer pairs. Standalone reference and event links require the same
quoted target in substantive citations from different originals with different
content. A short yes/no is eligible alongside a substantive cited question.
Corrections publish through claims analysis and remain context in exchange
comparisons. One-sided citations remain
unresolved/partial. Broad topics and ambiguous references remain available in
the report. Temporal output caps at 250 candidates, with omissions and partial
coverage disclosed.

`session_summaries` sends selected messages through shared content processing in
inferred-session chunks, up to 16,000 characters/chunk and 20 chunks/run. Each
summary discloses its chunk and selection in the saved run. Oversized or missing
chunks limit coverage. This adapts [iMessageLLM](https://github.com/aaronspindler/iMessageLLM)
without two-person reduction or inference-boundary bypass.

## Message CSV imports

Pandect imports direct conversations from the iMessage and WhatsApp CSV
profiles below. Previously retained HTML source documents, messages, and
imported context remain readable. Use CSV to prepare a new import.

Both profiles use `imessage_import` and result key `imessage`, with parser
versions `imessage-csv-v1` and `whatsapp-csv-v1`. The fresh pre-release
database/queue baseline applies. Earlier serialized formats require a new review
from the retained source. Source files preserve their bytes and historical
provenance.

Choose **Add evidence → Import records → Import message CSV** or a retained
Document's **More** action. Upload or select one CSV and confirm its export timezone.
When starting from a retained document, the form shows that CSV first. Open
**Limit by date** to enter a **Start date**, an **End date**, or both. Both
dates include the entire calendar day in the confirmed export timezone, including
when a source timestamp carries its own offset. Leaving both fields blank reviews
all dates. Pandect detects the profile from the exact header set and shows it
in review. Each profile accepts its headers in any order.

**iMessage (18 columns)** uses `Chat Session`, `Message ID`, `Message Date`,
`Delivered Date`, `Read Date`, `Edited Date`, `Deleted Date`, `Service`, `Type`,
`Sender ID`, `Sender Name`, `Status`, `Replying to`, `Subject`, `Text`, `Reactions`,
`Attachment`, and `Attachment type`.

**WhatsApp (15 columns)** uses `Chat Session`, `Message ID`, `Message Date`,
`Sent Date`, `Type`, `Sender ID`, `Sender Name`, `Status`, `Forwarded`,
`Replying to`, `Text`, `Reactions`, `Attachment`, `Attachment type`, and
`Attachment info`.

Both support UTF-8 with an optional BOM, quoted commas, and multiline text.
No message-row ceiling applies. Message CSV files allow 100 MiB, separately
from ordinary uploads. Fields allow 131,072 characters and each message allows
at most 1 MiB UTF-8. Preparation,
review and publication stream rows rather than accumulating conversations in results.

Background preparation creates no people/messages. Review shows contact labels,
counts, times, text and excluded-row reasons. In each participant's **Person**
chooser, pick a case person or choose **Create a person** and enter their
details. Names/numbers are unverified
suggestions, never automatic matches or split names. New person records can use a
name, phone number or email; selecting an existing person needs no account phone number.

Confirm one direct conversation. Multiple chats/incoming sender IDs are rejected;
group/mixed imports are unsupported. Chat labels alone identify no participants.
Outgoing uses account→other; incoming reverses them. Changing the timezone or
dates requires a fresh review, which can use the same retained CSV. Missing times
stay unknown in unrestricted imports. A date-limited import excludes messages
without a usable date and counts messages outside the range separately.
Malformed, ambiguous, or nonexistent local times, attachment-only rows,
WhatsApp system notifications, and unsupported directions are excluded.
The full CSV remains available for source review.
Confirmation publishes only ready messages from the reviewed selection.

Publication queues built-in methods and eligible checks on the
[case's selected AI backend](cases.md#ai-backend) for newly added messages.
Publication captures those execution settings when it queues analysis.

Confirmation queues method 2, separate from the immutable preview run. Publication
rechecks edit access/integrity and atomically creates requested people, messages,
contexts and results. Repeated confirmation/retries create no duplicates. Conflicting
source-row interpretations preserve originals. Document/row identity, as in HTML,
does not deduplicate distinct exports by native IDs.

Method 2 retains compact summaries/digests of reviewed interpretation and excerpts
from up to three initial rows. Ordinary review reads that retained summary;
automatic updates read only authorized run progress and offer a link when review
is ready. **Review excluded rows** and **Review all message rows** deliberately
read the checksum-verified original and show 25 matching rows using the retained
timezone, date bounds and text limits. Confirmation queues publication using
the prepared summary. The publication worker rechecks the complete original and
reviewed interpretation before publishing and reports errors on the import
status page. Row review and confirmation share a limit of 10 per minute per
user and 30 per minute per case across web processes. Excess requests return 429
before reading the source or queueing publication.
Summary pages and progress remain available. Eligibility/time changes need
fresh review. Result item/byte ceilings never truncate imports: messages are not
individual result items. Bounded publication groups share one transaction and
roll back entirely on conflict/failure. The completed import shows how many
messages were added, reused or excluded. **View conversation** opens the message
stream at the first newly published message in the export, or at a retained
message from the same source and conversation when all rows were reused. The
retained CSV, row review, and linked new messages remain available in disclosures
with separate pagination. Messages from both services appear together when
the investigator maps their exports to the same case-person pair.

Nullable `CommunicationContext.import_run` retains publishing runs; apply its
additive migration before updated web/workers. Existing messages/contexts stay
unchanged. Within `imessage_import`, historical method-1 results/local-check
membership remain readable; old previews need fresh reviews, and queued method 1
needs a method-2 run.

Decoded **Text** is preserved untrimmed with source row/exact character range;
CSV bytes stay exact. **Message ID**/**Replying to** retain native references;
**Chat Session** stays a label, not a native thread ID. **Info → Imported source
context** shows service, direction, contact claims, reactions, attachment
references, and other supplied fields. WhatsApp's `Sent Date`, `Forwarded`,
and `Attachment info` stay in this metadata. `Message Date` determines the
message time. Reactions create no messages, markers recover no history, and
attachments are never opened or fetched.

Publication atomically queues analysis only for newly created messages. Previews,
excluded/reused rows start no batch. **Follow message analysis** shows progress,
then check counts and **View conversation** after the work settles. If a
date-limited import reuses every row, **View conversation** opens a retained
message within the reviewed export-timezone range. Insufficient input has its
own count alongside completed checks. Incomplete coverage and stopped work
remain visible. **Analysis details** opens preparation and checks.
**Run import analysis again** creates a new batch under the case's current
analysis configuration and approval. Historical imports receive no automatic
backfill.

The suite covers identifiers, dates, keywords, sessions, timing, reported writing
profiles, repeated wording, and temporal exchanges. Eligible AI checks add topic
suggestions, claims, session summaries, and semantic exchanges.
Writing profiles separate reported senders. Comparisons split
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

Automatic comparisons can include active same-case material with matching
participants or a retained native thread. Timing methods use bounded windows
and retain exact sources. A case owner's approval also covers relevant history
used by approved external methods. Results disclose the scope used.

Automatic models use retained execution settings and quotation validation. Missing
configuration, drift, capacity and provider/output errors remain visible/retryable,
preserving messages and independent checks. Preparation and execution check
the retained route and approval. Failures stay with their original run.
Preparation/execution recheck verified editing access. Cursor,
runs and continuation commit together; retries queue no duplicates. After confirming
the old worker stopped, rerun failed preparation from the import page; individual
failures use normal Processing recovery.
