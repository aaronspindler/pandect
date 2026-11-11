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
the conservative supported set: com, org, net, edu, gov, mil, int, io, ai, co, uk,
ca, au, de, fr, nl, ch, se, es, it, jp, info, biz, app, dev, me, tv, us, eu, nz,
za, in, br, mx, sg, hk, ie, be, dk, no, fi, pl, test, example, invalid, or onion.
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

`sessions` orders selected messages by reported event time, breaking equal-time
ties by retained message/source IDs. Missing-time messages form unresolved
singletons and never bridge timed exchanges. Source-native participant context,
when present, remains scoped to its retained export. Otherwise same-case reported
participant pairs determine a comparison stream; these links do not verify
identity. The default gap boundary is 120 minutes; `gap_minutes` supports
1–10,080 minutes. A greeting after a farewell and at least five observed minutes
is an optional disclosed content boundary (`greeting_boundaries`).

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
and FaceTime using recorded participant/time context. `window_minutes` defaults
to 180 and supports 0–10,080; zero compares only equal recorded times. Content wording overlap is labelled as a separate
weak cue. Metadata-only calls supply ordering, not invented speech. Optional
`semantic_links` uses model-based two-sided quotation citations to propose
continued topics, events, and earlier references. A completed semantic candidate
needs exact citations to at least two distinct selected sources. One-sided or repeated citations to one source retain an
unresolved suggestion and a partial result rather than becoming a completed
link. Temporal adjacency and shared topics do not prove the same event or
causation; “that place” can retain several possible referents. Temporal output is
limited to 250 candidates with omitted counts and partial coverage disclosed.

`session_summaries` sends only inferred-session/message-boundary chunks to the
shared content path: at most 16,000 characters per chunk and 20 chunks per run.
Every summary states its chunk/source selection. A missing or oversized chunk
must not silently become an apparently complete history. These concepts adapt
[iMessageLLM's documented workflow](https://github.com/aaronspindler/iMessageLLM)
without its two-person reduction or local-provider inference path.

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

`sender_map` explicitly maps each raw source label to an existing same-case
Person UUID. `recipient_map` explicitly maps each sender label to its selected
recipient Person UUID when the source lacks a recipient field. Optional
`span.recipient` uses its own explicitly mapped source label. No label is reduced
to “Them,” and no person or recipient is guessed from “Me,” a display name,
timing, or a two-person assumption. Group labels retain full source context;
the existing two-person message relationship is a reported selected association,
not an assertion that the source had only two members.

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

Preparation writes no messages. At current-authorized atomic publication,
`apply_import` creates each mapped message and immutable `CommunicationContext`
with the retained document, checksum, parser version, timezone, row and exact
HTML character range. Unresolved rows stay visible in the partial result for
recovery. Whole publication rollback removes newly created messages/context.
Retries reuse the same document/row identity; a conflicting mapping or content
interpretation is rejected for review instead of replacing the earlier original.
Each mapped body is stored once in the retained result as the finding text used
for publication. The shared analysis result limit still applies to the complete
result.

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
