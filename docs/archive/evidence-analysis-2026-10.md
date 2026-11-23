# Archived evidence analysis and extraction catalogue

Scored **2026-10-27** against the **2026-10-26** worktree; archived **2026-11-15**.
Scores and implementation notes preserve that snapshot, not current estimates
or availability. [Analysis](../analysis.md) owns supported operations; the
[roadmap](../evidence-analysis-roadmap.md) owns proposed priorities.

## Scoring: investigative value and implementation ease

Independent **value** and **ease** scores range from 1–5 (higher is better/easier).
They are approximate product/engineering judgments, not accuracy, research,
probabilities, or effort estimates. Do not multiply them into a ranking.

| Score | Investigative value | Implementation ease |
| --- | --- | --- |
| **5** | Foundational: unlocks otherwise inaccessible evidence or broadly enables search, connections, chronology, or source assessment. | Easiest: straightforward queries/calculations on existing fields with limited additional integration. |
| **4** | High: regularly produces actionable leads, useful corroboration, or substantial review savings. | Relatively easy: bounded conventional parsing, statistics, or comparison with modest integration and clear validation. |
| **3** | Useful: adds context, organization, or review efficiency, but usually needs other evidence to advance a finding. | Moderate: an established approach with meaningful pipeline/provider integration, source anchoring, review, and evaluation. |
| **2** | Situational: narrower usefulness or substantial ambiguity limits dependable investigative benefit. | Hard: several missing prerequisites, substantial data/relationship work, or difficult reliability evaluation. |
| **1** | Limited: little dependable incremental benefit in a general investigation; potentially useful in exceptional cases. | Hardest: a new modality or research-sensitive reliability problem with substantial implementation uncertainty. |

Value assumes usable inputs in a general investigation. Ease includes ingestion,
provenance, authorization, recovery, and validation. The baseline lacked shared
retained results, a Gateway client, and geocoder; workers ran diagnostics only.
Shared foundations may reduce later effort; overlapping scores are not additive.

Combined scores include harder work: visual near-duplicates, scanned OCR, and
face-quality/false-match/inconclusive evaluation. Outputs remain candidates
unless independently established. Tables propose scope; current support is
documented separately.

## Image

[Extraction](../analysis-extraction.md) owns supported decoding, metadata, OCR,
and plate profiles; the table proposes broader scope.

| Capability and use | Value | Ease | Inputs | Proposed output | Limits |
| --- | --- | --- | --- | --- | --- |
| Metadata extraction: investigate capture context | 4 | 4 | One supported image with embedded metadata | EXIF/XMP fields, orientation, device/software claims, capture times, GPS candidates | Metadata may be absent, edited, stripped, or lack a timezone; capture time differs from upload time. |
| OCR and handwriting recognition: search visible text | 5 | 2 | Image containing legible text | Text, bounding boxes, reading order, language, uncertain characters | Blur, scripts, layout, and handwriting affect results; retain original crops and alternatives. |
| Face detection and grouping: find repeated appearances | 4 | 2 | One image for detection; multiple case images for clustering | Face regions, quality flags, candidate groups, representative crops | Groups can combine different people or split one person; clustering does not name anyone. |
| Face reference comparison: assess possible identity links | 4 | 2 | Face crops and explicitly supplied case reference images | Ranked candidates, similarity scores, supporting regions | Allow no match; scores are not identity probabilities. Reference attribution may itself be uncertain. |
| Licence-plate extraction: connect possible vehicle sightings | 4 | 2 | Images with visible plates | Plate crops, candidate strings, character alternatives, visible jurisdiction clues | Preserve ambiguous readings such as B/8; format plausibility cannot recover unreadable characters or prove vehicle identity. |
| Vehicle, object, and scene recognition: find material for review | 3 | 3 | One image; comparison images for repeated objects | Candidate vehicle attributes, objects, clothing details, signs, scene labels with regions | Occlusion and lookalikes limit specificity; visible attributes do not establish ownership or a person's role. |
| Landmark/place clues: suggest locations | 3 | 1 | Image and supplied reference imagery or an explicitly selected lookup source | Candidate landmarks/places and matching visual features | Generic architecture and outdated references mislead; weak resemblance cannot establish coordinates. |
| QR/barcode decoding: recover identifiers | 3 | 4 | Sufficiently clear code region | Raw decoded value, code type, region, parsed URL/identifier | Decoded content remains untrusted; do not automatically visit links or execute content. |
| Screenshot/photographed-document extraction: recover visible structure | 4 | 2 | Chat screenshot, receipt, form, or photographed page | Text blocks, tables, apparent messages, visible participant/time labels | Cropped context, spoofed interfaces, and missing metadata prevent native thread reconstruction. |
| Duplicate/near-duplicate detection: find reused imagery | 4 | 3 | Multiple case images | Exact-byte groups, visual similarity candidates, crop/resize/recompression relationships | Exact-byte grouping is implemented; visual comparison remains proposed. Similar images need not be the same capture. |
| Quality/manipulation triage: prioritize inspection | 2 | 1 | Original file; optional comparison versions | Blur/exposure flags, metadata inconsistencies, suspected edits or compositing regions | Editing and compression can create similar artifacts; neither an alert nor its absence establishes authenticity. Enhanced pixels remain derivatives. |

Face comparison needs task-specific evaluation. NIST distinguishes false matches
from missed matches and documents effects of image quality and demographic
variation. Performance in one comparison setting cannot simply be assumed in
another. See [NIST FRTE 1:1 Verification](https://pages.nist.gov/frvt/html/frvt11.html).

## Audio

Methods also apply to identified Phone call/FaceTime audio additions.
[Extraction](../analysis-extraction.md) owns WAV/transcription profiles.

| Capability and use | Value | Ease | Inputs | Proposed output | Limits |
| --- | --- | --- | --- | --- | --- |
| Technical metadata and quality checks: assess usable material | 3 | 4 | One supported audio file | Decoded duration, codec, channels, sample rate, clipping/noise/dropout flags | Compare with supplied metadata; supported, resource-bounded decoding is needed. |
| Speech/activity segmentation: navigate recordings | 3 | 3 | Decodable audio | Speech, silence, overlap, and uncertain intervals | Noise, music, and quiet speech can be confused; silence does not establish absence of relevant activity. |
| Timestamped transcription: search spoken content | 5 | 3 | Speech-bearing audio | Verbatim-oriented transcript with word/segment times and uncertain spans | Accents, names, overlap, and poor audio cause errors; retain unintelligible spans rather than inventing text. |
| Language identification and translation: understand multilingual material | 4 | 3 | Audio or a reviewed transcript | Segment language candidates and aligned translations | Code-switching and short segments are difficult; translation stays separate and may change meaning/style. |
| Speaker diarization: separate turns | 4 | 3 | Audio with one or more speakers | Anonymous speaker labels, intervals, overlap markers | Speaker A is local to a result; it does not establish identity or continuity across recordings. |
| Reference-voice comparison: investigate speaker continuity | 4 | 2 | Suitable speech segments and supplied reference recordings | Candidate similarities, comparison intervals, quality/coverage information | Channel differences, short samples, imitation, and processing confound results; no definitive identity claim. |
| Sound-event detection: find relevant background events | 2 | 3 | Audio with environmental sounds | Timestamped alarm, engine, door, impact, or other sound candidates | Similar sounds have different causes; labels alone cannot locate a recording or establish what happened. |
| Content extraction: find topics, claims, events, commitments | 4 | 2 | Transcript linked to audio | Entities, attributed claims, proposed actions, cited summaries and intervals | Transcription errors propagate; plans and statements remain attributed speech, not verified events. |
| Recording comparison and transcript alignment: find reuse/disagreement | 4 | 2 | Multiple recordings, or audio plus supplied transcripts | Duplicate/overlap candidates, aligned passages, omissions, additions, discrepancies | Differences may reflect excerpting or transcription practice; identify each version and preserve disagreements. |

## Document

Native text/PDF and vehicle CSV extraction are bounded; broader formats and
structure need separate support.

| Capability and use | Value | Ease | Inputs | Proposed output | Limits |
| --- | --- | --- | --- | --- | --- |
| Native text and OCR: make contents searchable | 5 | 2 | Supported text/PDF/office formats or scanned pages | Text, page/section anchors, reading order, coordinates, extraction method | Native text and rendered appearance may disagree; OCR introduces errors. |
| Layout, tables, and forms: recover structure | 4 | 2 | Pages with headings, tables, lists, or fields | Section hierarchy, rows/cells, field/value pairs, page regions | Merged cells, footnotes, and columns need review; preserve units and headers. |
| Metadata/embedded-content inspection: understand file context | 3 | 2 | Original file with metadata or embedded objects | Author/software/time claims, links, comments, attachment/object inventory | Metadata is editable; macros/attachments must not execute. Extracted objects retain parent provenance. |
| Document classification: organize collections | 3 | 3 | Extracted text/layout or page images | Candidate invoice, statement, report, correspondence, or log labels | Mixed documents may need section-level labels; classification is a retrieval aid. |
| Entity/date/identifier/amount extraction: connect records | 5 | 2 | Readable text/tables | Names, organizations, contact details, identifiers, dates, amounts, currencies, source spans | Ambiguous dates, reused identifiers, and missing units require context and alternatives. |
| Transaction/event extraction: examine reported activity | 4 | 2 | Statements, invoices, receipts, logs, or structured records | Candidate transactions/events, parties, dates, amounts, totals, reconciliation discrepancies | Invoices do not prove payment; distinguish balances, transfers, subtotals, and duplicates before aggregation. |
| Summaries and claim comparison: navigate long material | 4 | 2 | One document or related collection | Cited summaries, attributed claims, supporting/conflicting passages, open questions | Preserve qualifications, negation, and uncertainty; source assertions remain distinct from findings. |
| Version/passage/template comparison: identify reuse and changes | 4 | 3 | Multiple related documents | Text/layout differences, reused sections, likely templates, candidate version families | File timestamps do not establish revision order; common wording does not establish authorship. |
| Handwriting/signature inspection: investigate visual similarities | 3 | 1 | Legible regions; supplied reference specimens for comparison | Detected regions, transcription candidates, visual comparison features | Recognition, signature appearance, and linguistic style are different tasks; none automatically authenticates a signature. |

## Text

Text preserves server-received browser content; use Document for exact file
bytes. Methods also apply to origin-labelled extraction, OCR, messages, and
transcripts.

| Capability and use | Value | Ease | Inputs | Proposed output | Limits |
| --- | --- | --- | --- | --- | --- |
| Language and translation: understand content | 4 | 3 | Text or mixed-language segments | Language candidates and aligned translations | Short/slang-heavy text is ambiguous; translation must not replace originals or enter an unlabeled style comparison. |
| Entity/indicator extraction: find useful references | 5 | 3 | Text with surrounding context | Names, organizations, phones, emails, URLs/domains, addresses, coordinates, identifiers, dates, amounts | Mentions do not establish ownership/association; retain exact spans and ambiguous formats. |
| Topics, keywords, summaries: organize material | 4 | 3 | One text or collection | Topic labels, key phrases, cited summaries, related passages | Topics and sentiment of wording describe content, not character, intent, or mental state. |
| Events, relationships, commitments, claims: structure assertions | 4 | 2 | Text describing actors/actions, dates, or relationships | Attributed subject/action/object candidates, times, plans, questions, claims | Preserve tense, negation, hearsay, hypothetical language, and unresolved relative dates; a plan is not an event. |
| Semantic search/similarity: retrieve related material | 5 | 3 | Authorized case corpus and query/example text | Ranked passages, related-record candidates, source references | Similarity can follow topics rather than shared events; authorization must also cover indexes/results. |
| Copied wording/templates: identify source dependence | 4 | 4 | Multiple texts or reference passages | Exact/fuzzy matches, quotations, boilerplate, recurring phrases | Common phrases are weak attribution signals; copied agreement is not independent corroboration. |
| Writing-style analysis: describe expression | 3 | 3 | Authored samples with source/channel context | Style profiles, similarity candidates, shifts between samples | Apply the sample checks and inconclusive outcomes in [Writing styles](#writing-styles-and-candidate-authorship). |

## Location

Ordinary Locations have no subject association; list/timeline position does not
make them a person's or vehicle's journey.

| Capability and use | Value | Ease | Inputs | Proposed output | Limits |
| --- | --- | --- | --- | --- | --- |
| Address normalization/place matching: group equivalent descriptions | 4 | 3 | Addresses; reference data where needed | Parsed components, normalized derivatives, candidate equivalent places | Keep originals; units, aliases, and historical addresses can distinguish similar locations. |
| Geocoding/reverse geocoding: connect addresses and coordinates | 4 | 3 | Address or coordinates plus an explicitly selected geocoder/dataset | Candidate coordinates/addresses, match granularity, lookup provenance | A centroid is not an observed coordinate; external queries disclose location data. See [configured geocoding](../analysis-locations.md#geocoding-configuration-and-estimate-selection). |
| Time/coordinate quality checks: surface interpretation problems | 4 | 3 | Observations and source context | Missing-time flags, address/coordinate disagreements, duplicate candidates, time ambiguities | Basic ranges are already validated; decimal precision does not establish accuracy. Unknown event times stay unknown. |
| Spatial grouping/geographic context: organize places | 3 | 3 | Coordinates/reviewed geocodes; relevant reference layers | Candidate clusters, neighbourhood/road/venue context, spatial summaries | Maps and boundaries change; a shared building can contain unrelated occupants. Clusters do not establish visits by a common subject. |
| Proximity/geofence queries: find relevant observations | 4 | 4 | Coordinates, area/reference point, explicit distance assumptions | Distance estimates, candidate inside/near/outside observations | Boundary decisions depend on uncertain positions; missing accuracy information must remain visible. |
| Conflicting-observation review: compare source claims | 4 | 3 | Records explicitly connected to the same claimed event/place | Side-by-side address, coordinate, or time discrepancies | Similar timestamps alone do not make unrelated locations contradictory; retain alternative interpretations. |

## Person location

Methods extend across person-associated observations. Association does not prove
presence or identify a device's carrier.

| Capability and use | Value | Ease | Inputs | Proposed output | Limits |
| --- | --- | --- | --- | --- | --- |
| Candidate journeys/stops: reconstruct possible movement | 4 | 2 | Ordered, timed observations with coordinates/geocodes | Candidate journeys, stop intervals, origins/destinations, gaps | Sparse samples cannot establish a continuous route, transport mode, or exact departure/arrival. |
| Frequently visited places: find recurring locations | 4 | 2 | Repeated timed observations with sufficient coverage | Place clusters, candidate visits, weekday/time recurrence, observed dwell intervals | Visits differ from samples; presence between reports is inferred. Frequent does not automatically mean home or workplace. |
| Routine/origin-destination comparison: review changes | 3 | 2 | Multiple journeys across comparable periods | Recurring place pairs, time distributions, deviations | Coverage changes may explain differences; show the observation window and sampling. |
| Candidate co-location: find overlapping observations | 4 | 2 | Multiple subjects with comparable place/time precision | Proximity/time overlaps and supporting observations | Proximity is not a meeting, relationship, or shared activity; large venues and time uncertainty create false associations. |
| Movement anomalies: identify suspect interpretations | 4 | 3 | Timed observations, distance estimates, travel assumptions | Rapid transitions, inconsistent order, candidate data errors | Flag apparently impossible travel relative to assumptions; do not conclude deception or silently correct locations. |
| Appointment/claim comparison: inspect supporting/conflicting material | 4 | 2 | Locations plus messages, document calendars, or attributed claims | Candidate event/location matches, discrepancies, cited alternatives | Appointments may not occur; missing observations do not establish absence. |

## Vehicle location

Manual records have ordinary location fields. CSV has **addresses, not coordinate
columns**, plus speed, heading, trigger/time-summary text, event/receipt times,
and device labels; some remain only in original rows. Geospatial methods need
derived geocodes or additional coordinate sources.

| Capability and use | Value | Ease | Inputs | Proposed output | Limits |
| --- | --- | --- | --- | --- | --- |
| Candidate drives/trip segmentation: organize movement | 4 | 2 | Vehicle observations with sufficient times; spatial data for movement | Candidate trips, start/end observations, stops, durations, gaps | Sampling gaps and unknown ignition state limit boundaries. Vehicle/device movement does not identify the driver. |
| Stops/frequent destinations: identify recurring activity | 4 | 2 | Repeated timed observations and place estimates | Stop/visit clusters, dwell intervals, counts, recurring time windows | Zero speed does not establish parking or engine-off; one address can contain separate visits. |
| Route/distance estimation: compare possible journeys | 4 | 2 | Ordered coordinates/geocodes and an explicit road-network source | Observed-point displacement, candidate matched routes, estimated travel distances/times | Matching between samples is hypothetical; distinguish straight-line displacement, estimated road distance, and actual travel. |
| Speed/heading patterns: inspect telemetry | 3 | 4 | Retained speed/raw heading; positions and times for derived estimates | Reported-speed summaries, direction changes, separately labelled segment estimates | Reported speed differs from distance/time estimates. Speeding claims require reliable positioning and applicable limits, neither supplied by this analysis alone. |
| Recurring routes/origin-destination pairs: compare trips | 3 | 2 | Multiple candidate trips with comparable coverage | Repeated corridors/place pairs, timing patterns, route alternatives | Common endpoints do not prove the same route; geocoding/segmentation choices affect results. |
| Coverage/receipt delays: assess reliability | 4 | 5 | Event times, optional receipt times, ordered rows, source context | Gaps, out-of-order events, receipt-minus-event delays, duplicate candidates | A gap may mean no reporting rather than no movement; negative delays can reflect clocks or field semantics. |
| Device-association/sighting comparison: investigate conflicts | 4 | 2 | Device labels, explicit vehicle mappings, plate photos or other sightings | Inconsistent associations, candidate same-vehicle links, supporting/conflicting observations | Serial Number is not a VIN. Cloned plates, mapping mistakes, and moved devices remain possible explanations. |

## Text message

Each record holds one message and two reported person links. Bounded
[iMessage import](../analysis-communications.md#message-csv-imports) retains
supplied native context; richer relationships need supported inputs. Missing
fields stay unknown.

| Capability and use | Value | Ease | Inputs | Proposed output | Limits |
| --- | --- | --- | --- | --- | --- |
| Source-format extraction: retain message context | 5 | 2 | Supported exports/screenshots; new source-specific ingestion | Bodies, raw account/participant labels, event times/timezones, source anchors | Broader ingestion needs supported source profiles. Screenshot labels are not equivalent to native metadata. |
| Native thread/reply reconstruction: recover explicit structure | 5 | 2 | Source thread/message/reply IDs, group membership, attachments where available | Explicit thread, reply, attachment, participant links | Needs new inputs/model support; missing identifiers must not become asserted relationships. |
| Session segmentation: make streams navigable | 4 | 3 | Multiple messages with participants, times, content | Candidate session boundaries with reasons | Heuristics may split one exchange or combine several; retain alternatives and reviewer adjustments. |
| Topic continuation/reference linking: connect exchanges | 4 | 2 | Messages across sessions/channels with context | Candidate references, continued topics, shared entities, earlier-message links | Phrases such as “that place” can have several referents; the same topic need not mean the same event. |
| Content/summary extraction: identify leads | 4 | 3 | Message bodies and time/source context | Entities, plans, events, commitments, questions/answers, attributed claims, cited summaries | Distinguish proposed, cancelled, reported, and completed actions; preserve quotations and contradictions. |
| Timing/interaction patterns: describe observed exchanges | 4 | 4 | Ordered messages with participants and event times | Volume, apparent initiation, response-interval distributions, turn-taking | Missing exports, overlapping topics, and unknown read times prevent claims about actual response latency or intent. |
| Writing-style comparison: examine changes | 3 | 3 | Sufficient original messages by attributed sender/period/channel | Profiles, similarities, shifts, recurring phrases | Autocorrect, short messages, recipient, and copied text confound attribution; apply dedicated style guidance. |

## Phone call

**Metadata-only calls** provide reported participants, time, and duration where
present. Content methods need identified transcript/audio additions; supplied
transcripts do not prove exact speech.

| Capability and use | Value | Ease | Inputs | Proposed output | Limits |
| --- | --- | --- | --- | --- | --- |
| Call patterns: describe recorded interaction | 4 | 5 | Multiple records with relevant time/duration fields | Counts, reported-duration distributions, recurring time windows, participant-pair summaries | Unknown duration differs from zero; current records lack answered/missed status and network verification. |
| Cross-channel continuity: connect calls/messages | 4 | 3 | Calls and relevant case messages/records | Candidate before/after sequences, topic/event links | Temporal adjacency does not prove discussion content or causation. |
| Transcription/speaker turns: inspect recordings | 5 | 3 | Audio additions; optional supplied transcripts | Timestamped transcript candidates, anonymous turns, source intervals | Reuse Audio methods; recorded speakers can differ from linked call participants. |
| Content extraction: find plans/claims | 4 | 3 | Supplied or derived transcripts, ideally linked to audio | Topics, entities, commitments, event references, questions/answers, cited summaries | Attribute speakers only with support; metadata-only calls have no content to analyze. |
| Transcript/recording comparison: review corrections | 4 | 2 | Audio and one or more retained transcripts | Aligned passages, disagreements, omissions, version comparisons | State recording coverage and attachment identity; preserve every original addition rather than silently reconciling discrepancies. |

## FaceTime

FaceTime is a call with optional audio/transcript additions, without retained
video, screen share, or frames.

| Capability and use | Value | Ease | Inputs | Proposed output | Limits |
| --- | --- | --- | --- | --- | --- |
| Call/cross-channel patterns: place a call in context | 4 | 3 | Metadata and related case communications | Counts, reported durations, time patterns, candidate sequences | Apply Phone call's time/status and causal limits; do not infer who appeared on camera. |
| Audio/transcript analysis: extract retained content | 5 | 2 | Identified audio/transcript additions | Transcription, turns, topics, claims, commitments, cited summaries/comparisons | Apply Audio/Phone call methods; a transcript's visual description remains an attributed statement. |
| Future video analysis: examine visual material if later supported | 4 | 1 | New retained video inputs, provenance, decoding, frame/time support | Frame-linked face/object/OCR candidates, scene changes, audio/video alignment | **Requires new supported inputs.** No current video field/type exists; a call record alone cannot yield visual results. |

## Writing styles and candidate authorship

Describe **how samples are written** before comparing authorship. Linguistic
style differs from handwriting recognition and signature appearance.

| Capability and use | Value | Ease | Inputs | Proposed output | Limits |
| --- | --- | --- | --- | --- | --- |
| Style profiles: explain expression | 3 | 3 | Original authored text with language/channel context | Punctuation, capitalization, spelling variants, vocabulary, function words, sentence/message lengths, abbreviations, emoji, repeated phrases, greetings/sign-offs, code-switching | Report distributions/examples, sample sizes, and exclusions; not personality labels. |
| Candidate-author comparison: identify useful comparisons | 3 | 1 | Questioned text and explicitly supplied, attributed case reference samples | Ranked similarities/differences, supporting and contrary examples | Permit **insufficient evidence** and **none of the supplied candidates**; similarity is not identity proof or a calibrated probability. |
| Style changes: locate shifts for review | 3 | 3 | Comparable samples by time, recipient, channel, or document section | Feature changes, differing samples, intervals/sections to inspect | Audience, topic, editing, dictation, translation, collaboration, and tools can change style without changing the author. |
| Copied wording/templates: separate reuse from style | 4 | 4 | Multiple samples and identifiable quotations/templates | Shared passages, boilerplate, forwarded/quoted text, comparisons with exclusions | Copied language is not independent evidence of habits; common phrases remain weak attribution signals. |
| Anonymous style grouping: explore patterns | 2 | 2 | Sufficient comparable texts with unresolved authorship | Candidate groups and representative features/examples | Groups may follow topic, language, platform, or template; do not merge people or assign names from clusters. |

Sample preparation:

- Preserve supplied attribution and its uncertainty, including shared accounts.
  Disclose language, genre, topic, recipient, channel, date range, and amount of
  usable text. Compare original-language samples and similar contexts where possible.
- No universal minimum sample size is selected here. Short or repetitive samples
  may be uninformative; future methods must establish adequacy on relevant
  material instead of always returning a ranking.
- Identify quotations, signatures, forwarded text, templates, and copied passages;
  show exclusions. Do not treat every remaining difference as authorship evidence.
- OCR errors can mimic spelling habits. Speech recognition and transcribers add
  punctuation, capitalization, and spelling. Call transcripts can support spoken
  wording analysis, but their formatting does not establish a speaker's writing style.
- Keep translations, normalization, spelling corrections, and edits distinct from
  originals. A fluent model explanation is not validation of a comparison method.
  Do not infer protected traits, mental health, truthfulness, or personality.

Primary research distinguishes style from topic/genre effects: names and content
words can make texts resemble each other because they discuss the same subject.
Assess comparisons across topics as well as within them. See
[What represents “style” in authorship attribution?](https://aclanthology.org/C18-1238/)
and [The Topic Confusion Task](https://aclanthology.org/2021.findings-emnlp.359/).
These studies inform evaluation questions; they do not validate a Pandect method.

## Conversation extraction and linking

[iMessageLLM](https://github.com/aaronspindler/iMessageLLM) offers useful concepts
for organizing message streams. Its
[parser/segmentation code](https://github.com/aaronspindler/iMessageLLM/blob/14a46b9826ad2d8a0c0777d3cfd4394a49f3ce9a/process.py)
extracts a specific HTML export and proposes sessions using timing, turn-taking,
word overlap, and greeting/farewell cues. These are **inferred sessions**, not
native threads or explicit reply relationships. It reduces senders to “Me” and
“Them”; Pandect must preserve distinct source participants and uncertain person
associations.

Its [analysis workflow](https://github.com/aaronspindler/iMessageLLM/blob/14a46b9826ad2d8a0c0777d3cfd4394a49f3ce9a/ask_messages.py)
demonstrates conversation-aware chunking, questions across selected sessions,
synthesis, and cached answers. Adapt these concepts, not its direct local-model
execution or two-person assumptions. Dedicated stylometry and evidence-grade
result provenance are additional work; Pandect's integration requirements apply.

| Capability and use | Value | Ease | Inputs | Proposed output | Limits |
| --- | --- | --- | --- | --- | --- |
| Preserve native structure: recover source-recorded relationships | 5 | 2 | Retained export, source IDs, timezone, participant/reply/attachment/group metadata | Explicit links, source offsets, raw-to-normalized mappings | Needs richer ingestion/context support; missing fields stay unknown rather than being inferred from display names. |
| Infer sessions: create reviewable exchanges | 4 | 3 | Chronological messages with participant/time/content context | Boundaries and reasons, alternative groupings, reviewer adjustments | Timing/topic cues are heuristics; keep inferred sessions distinct from native thread identity. |
| Link conversations: follow topics/events across channels | 4 | 2 | Messages, call transcripts, related text with anchors | Candidate continuations, references, shared-event/participant links | Preserve competing referents; lexical overlap alone is weak evidence of the same event. |
| Extract interaction structure: clarify discussions | 4 | 2 | Ordered dialogue with attributed turns | Questions/candidate answers, proposals, commitments, cancellations, unresolved references | Sarcasm, quotations, concurrent topics, and missing messages limit interpretation. |
| Summarize/ask source-backed questions: navigate histories | 5 | 2 | Selected sessions with coverage and stable source references | Session summaries, topic histories, comparisons over time, cited answers and contrary passages | Chunk limits require explicit coverage; unanswered questions or omitted periods must not become invented conclusions. |
| Compare interaction patterns: describe observed changes | 4 | 4 | Comparable exports with times, participants, known coverage | Counts, session durations, apparent initiations, turn-taking, response intervals | Show denominators, missing periods, and session assumptions; do not infer motives, relationship quality, or emotional state. |

Preserve source timezones when available; collection/upload time is not message
time. Future sources may supply group membership, reactions, edits, deletion
markers, forwarding, and attachments. Retain that context without implying that
a deletion marker recovers deleted message content.

## Interpreting locations, drives, and frequent places

Trip segmentation, stay-point detection, route matching, and pattern mining are
established trajectory-analysis topics. Movement between discrete observations
remains uncertain. The [Microsoft Research trajectory overview](https://www.microsoft.com/en-us/research/project/trajectory-data-mining/)
provides background; the following are proposed interpretation requirements,
not an expansion of the bounded methods documented in [location analysis](../analysis-locations.md).

1. **Establish coverage first.** Separate event, receipt, collection, and upload
   times. Show timezone interpretation, windows, gaps, duplicates, geocoding
   quality, and missing accuracy data. Current locations lack a dedicated
   accuracy radius or observed-presence interval.
2. **Separate observations and estimates.** Distinguish recorded coordinates,
   geocoded candidates, inferred stops, straight-line displacement, and estimated
   routes. Do not present a continuous verified journey through gaps. Preserve
   map/geocoder versions and relevant segmentation assumptions.
3. **Count visits, not samples.** Twenty reports from one stationary period are
   not twenty visits. Visit segmentation needs explicit time/distance assumptions
   and gap handling. State observed bounds and assumptions for dwell estimates;
   missing reports cannot prove continuous presence.
4. **Compare like coverage.** Frequent-place rankings, recurring drives, and
   routine changes need observation periods and sampling coverage. More reports
   can mean a more active logger rather than more activity.
5. **Keep subject links uncertain.** Vehicle position does not identify a driver;
   person-location associations do not prove presence. Proximity is not a meeting.
   Frequent places are clusters, not automatically home, work, or personal roles.
6. **Respect source semantics.** Asset/Serial Number are device labels; Trigger
   does not establish engine state; Time Summary is retained text, not a parsed
   duration. The importer requires explicit timezone/vehicle interpretation and
   does not geocode. New interpretations must cite the exact retained CSV rows.

## Analysis across evidence

Workflows stay within one authorized case. Label links as source-recorded, shared
values, proximity, or model similarity; cross-case matching is not assumed.

| Capability and use | Value | Ease | Inputs | Proposed output | Limits |
| --- | --- | --- | --- | --- | --- |
| Entity/identifier linking: find related records | 5 | 2 | Extracted names, phones, emails, plates, accounts, addresses, identifiers | Candidate same-entity links, evidence-backed graph edges, conflicting attributes | Shared/reassigned identifiers and namesakes prevent automatic identity/ownership conclusions; preserve raw values and normalization. |
| Unified event timeline: compare reported activity | 5 | 2 | Event times, extracted time expressions, communications, observations | Events with anchors, precision/intervals, planned/reported/observed distinctions | Resolve relative dates only with context; retain conflicting clocks and uncertain order. Upload order is not event order. |
| Conversations linked to journeys: compare plans and observations | 4 | 2 | Place/time references in messages/calls plus person/vehicle locations | Candidate plan/trip/visit links and discrepancies | A proposal is not an actual trip; a vehicle sighting does not identify a sender as driver. |
| Repeated appearances/places: connect sightings | 4 | 2 | Image regions, plate candidates, references, locations | Candidate person/vehicle/object/place groups with supporting records | Similarity, proximity, and explicit links are different reasons; avoid transitive identity assumptions. |
| Duplicate-source detection: assess apparent corroboration | 5 | 2 | Hashes, similar media, matching text, citations/import provenance | Duplicate families, copied-source candidates, source-independence questions | Different publications may derive from one original; repeated claims are not automatically independent support. |
| Claim corroboration/conflict review: compare assertions | 5 | 2 | Attributed claims and relevant observations/documents | Supporting/conflicting excerpts, incompatible values, unresolved alternatives | Agreement does not establish truth; absence is not a contradiction without justified coverage expectations. |
| Source-backed case summaries: assemble reviewable findings | 5 | 2 | Explicit evidence/result selection | Cited summaries, open questions, candidate leads, chronology, gaps | Separate observations, suggestions, and investigator conclusions; include contrary material and coverage limits. |

## Shared requirements for future analysis

Follow [AGENTS.md](../../AGENTS.md) for original preservation, case authorization,
uncertainty, processing bounds, and failure handling. Additional analysis must:

- Retain input/attachment IDs, checksums, selections, parameters, and processing
  versions across reruns. Derived values never replace originals.
- Anchor text to spans/messages, documents to pages/regions, images to boxes,
  audio to intervals, and imported observations to rows. Expose supporting and
  conflicting inputs, coverage, and exclusions.
- Keep source assertions, calculations, model suggestions, and investigator
  conclusions distinguishable. Similarity is not a probability of identity,
  truth, causation, or ownership.
- Use the shared inference boundary: configured private runtimes by default,
  external inference only through separately approved Cloudflare AI Gateway,
  and no automatic fallback. Local OCR remains local. See [local analysis](../local-analysis.md).
- Validate representative supported inputs and failures before quality claims.
  This catalogue does not establish suitability or accuracy.
