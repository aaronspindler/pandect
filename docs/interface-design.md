# Investigator interface specification

**Status: core interfaces implemented; validation results are separate.**
This guide owns screen behavior; [DESIGN.md](../DESIGN.md) owns shared design and
accessibility rules; [validation](validation.md) owns checks and procedures.
Generated mockups are references, not rendered evidence.

## Navigation and screens

Cases open to **Case Tools**: recent evidence/findings and links to the Findings
catalogue, Map, Chronology, Conversations, Connections, People, Vehicles,
Selections, and reports. Native **Case details** discloses supporting information
and owner-only Settings. Investigators need no staff access. Graphs, inferred
event timelines, ownership transfer, and self-service account deletion remain
planned; show only implemented destinations.

All investigator screens, including All cases and account settings, share the
rail and controls. Case screens link directly to **Case Tools**, **Evidence**,
and **Processing**. The rail becomes a compact top bar on narrow screens. Mark
the current destination and link the case name on supporting pages. Case Tools
is a full page. Disclosures work without JavaScript.

Cases, People, Vehicles, and Evidence use shared whole-row links with readable
titles and secondary metadata. Detail pages and forms share the reader's heading,
action, divider, and spacing styles. Info/More use the shared
[icon-control and tooltip rules](../DESIGN.md#visual-foundations) when their words
disappear on mobile.

People and Vehicles retain list searches and pages through creation, editing,
deletion, and cancellation. Shared forms name choice groups with native legends;
error links focus the affected control or group.

| Screen | Content and primary action |
| --- | --- |
| All cases | Searchable owned/shared cases with roles; **Create case**. |
| Case Tools | Default case home; recent evidence and findings, supporting views, reports, and **Add evidence** for owners/editors. |
| Evidence | Active records, search and disclosed filters; **Add evidence** for owners/editors. |
| Processing | Active checks and imports, paginated history, recovery links, and owner/editor **Rerun all analysis**. |
| Evidence reader | Original and Findings together; downloads, Info, and authorized More actions beside the title. |
| Conversations | Reported participant pairs and message counts; chat reader, retained-export filter, inline message details, and links to individual evidence. |
| Findings catalogue | Searchable retained source-linked results, with filters and separate review states. |
| People / Vehicles | Searchable case records; owner/editor creation and editing, viewer inspection. |
| Case settings | Owner-only name/access management and a separate delete section. |
| Case details | Owner, timestamps, role, and saved read-only description for all case members. |
| Invitation | Named recipient confirmation after authentication and email verification; **Accept invitation**. |
| Account | Allauth email/password management, timezone preference, and POST sign-out, with the shared shell. |

Public `/` serves marketing anonymously and redirects signed-in visitors to All
cases. Product routes require an active authenticated account and verified email.
Preserve safe local return destinations through sign-in/verification. Email
settings show address/status and require named removal confirmation.

Display times and date/time fields in the device-detected timezone or the user's
**Account → Timezone** choice. Label the timezone and respect local midnight
boundaries. Source-export timezone choices remain explicit and independent.

## Visual reference and density

The synthetic [quiet workspace reference](design/quiet-evidence-reference.png)
guides the rail, collection, and reader. Actual processing output takes precedence
over illustrative findings; earlier references are historical.

![Quiet evidence workspace](design/quiet-evidence-reference.png)

Follow [DESIGN.md](../DESIGN.md#layout-and-density) for panes, breakpoints, rows,
and density. At default zoom, target at most `164px` for heading/Add/search/Filters
and `44px` for pagination. Use about `65ch` for original text; group supporting
metadata in Info.

Use 25 records per page and one whole-row link. Let long titles/larger text
expand rows; never crop the accessible name. Mobile opens the canonical detail
with Back to evidence. Use document scrolling at short heights or high zoom.

## Cases and investigative records

Case creation asks for a name and assigns the current user as owner server-side.
Search accessible names/descriptions with owned/shared filters and stable
newest-updated ordering. Empty cases offer a next action; empty search offers
Clear filters. Only owners edit metadata or collaborators; saved descriptions
remain read-only.

Person details show reported social accounts and POIs. Owners/editors manage
accounts with confirmed removal; viewers inspect. Search includes platforms and
usernames. Disclose saved notes/history read-only. [People](people.md) and
[vehicles](vehicles.md) own identity, role, and deletion rules, including
retained-location protection.

Case deletion is a separate owner confirmation naming the case and inventory,
including people, vehicles, active/archived evidence, collaborators, and pending
invitations. Require an acknowledgement and **Permanently delete case** POST.
Explain that accounts remain, there is no in-app undo, and storage/backups may
retain copies; do not promise verified erasure.

## Evidence browsing and reading

Use ordinary links with `aria-current`. The selected title is the single `h1`;
the collection heading is lower-level. Canonical URLs contain case, allowlisted
type, and UUID. Preserve query, type, archive state, and collection page through
selection and return navigation.

Visible literal search accepts up to 200 characters across metadata, captured
text/messages, supplied transcripts, and supported completed extraction. Excerpts
link to originals/results; [analysis](analysis.md) owns extraction coverage.
**Filters** holds type and Active/Archived/All with **Apply filters**; reopen
non-default filters after navigation. Omit repeated ingestion dates from rows;
retain exact dates in history.

One reader header holds title, concise type/source/collection metadata, archive
state, and authorized download. Originals and source-linked Findings appear
together without tabs or jump navigation. Findings show coverage/status and
**Run checks** when empty; populated findings use More for reruns. Keep detailed
reviews on individual results instead of repeating “Awaiting review” on highlights.

Header disclosures:

- **Info** holds full source metadata, file details, saved descriptions, declared
  technical fields, record/integrity history, imported source context, retained
  attachment attribution, and processing history. History uses `results_page`
  independently of the collection's `page`; pagination keeps Info open.
- **More** holds permitted edits, archive/restore, additional analysis, and
  superuser deletion. Viewers can still open Info and download originals.

Native disclosures support keyboards and no JavaScript. Reveal invalid controls
before focusing their fields.

Every case role can download privately. Escape text; never embed untrusted
documents as application HTML. Missing originals retain metadata and show safe
recovery. Checksums remain fully selectable, with optional copy. Metadata edits
never change originals; changed originals become new evidence.

Archive/restore are explicit owner/editor POSTs. Archiving keeps the selected
detail visible, with Archived and **More → Restore**, while removing it from
the Active list. Current members can still inspect/download archived material. Individual
permanent deletion is reserved for superusers with current case access; show
originals, imported derivatives, analysis, and retained files in a complete,
paginated inventory with a full download. Bind confirmation to that inventory
and require another review if it changes. Cancellation preserves list context;
inconsistent cross-case links block deletion without exposing foreign records.
See [evidence](evidence.md) for the deletion and private-storage contract.

## Conversation reading

**Case Tools → Conversations** lists reported participant pairs, message counts,
and latest recorded times, loading 25 more pairs when scrolling to the end.
Opening a pair shows a compact chat reader loading 50 messages at a time, oldest
recorded time first and unknown times last. Put sender and time on one wrapping
line, with reduced bubble padding and spacing. Keep sender names explicit and
participant alignment consistent; do not label either participant as the signed-in
user. Date separators and timestamps use the display timezone. Preserve message
whitespace and escape source content.

**All sources** includes imported and individually entered messages. A labelled
retained-export selector narrows the pair and resets pagination/selection. Keep
source attribution beside each message, preserve overlapping-export records, and
mark archived records. Explain the reported-participant grouping once rather
than suggesting a verified native thread or complete history.

Each message has native **Message details** for reported participants/time,
archive state, source, and imported context. Render details inline at every
screen size, without fetch/reload or JavaScript, preserving focus and position.

Beside each source, **Open evidence** opens the canonical reader and native
reply/thread lists. Its **View in conversation** returns to the exact message
and page, opens details, and marks the target beyond color. Preserve export and
conversation-list page throughout.

Filtering and evidence links use ordinary GET navigation. Scrolling near either
loaded boundary adds the adjacent page in place, keeping the export/list context,
deduplicating date headings and retaining focus and position when prepending.
Announce loading, completion, and failures; retain ordinary earlier/later links
for keyboard use, without JavaScript, and as full-page recovery after a failed
request. Each request uses the same authorized, bounded page route. Browser Back
preserves the original source/page context. Use ordinary page scrolling at every
screen size. Empty conversations and empty export-filtered results retain case
navigation and offer the appropriate evidence or clear-filter link.
See [evidence](evidence.md#reading-conversations) for interpretation limits.

## Evidence creation and batch uploads

**Add evidence** leads with mixed-file **Upload files**, then visible **Add a
record** links for text/locations/communications and **Import records** links for
vehicle locations and CSV messages. Single-record forms name the type and offer
**Change type → Choose type**. All ten types work without JavaScript; single-file
Document is the upload fallback.

Keep title/original fields visible. Disclose optional source/technical fields
on request, editing, or validation errors. Saved descriptions/notes remain
readable; new note authoring is retired. Retain citations and review reasons.

The limits are 100 batch entries, 1 GiB per file, and 10 MiB UTF-8 captured
text; show the application limits. Each file is a separate request. A batch
row shows filename, editable title/type, source overrides, and state. Browser type
hints are suggestions, not verification. Shared source fields follow pending
entries until individually overridden, including by blank values; **Use shared
details** resets an override. Saved/frozen entries retain submitted values.

| State | Behavior |
| --- | --- |
| Ready | Edit or remove before sending. |
| Uploading / Saving | Freeze submitted values; show byte progress, then saving until confirmed. |
| Added | Link to the retained record; never resubmit a confirmed success. |
| Failed | Preserve values and offer retry/removal; confirmed noncommitting validation errors permit correction with a fresh submission ID. |
| Outcome unknown | Resolve the original frozen submission ID/payload before editing or removing it. |
| Access removed | Stop queued writes and explain the loss of access. |

Pickers and drag/drop append files without replacing pending edits. Block new
files and shared edits while uploading or awaiting confirmation. Saved entries
count toward the queue limit; removing all pending files restores the empty
picker. **Stop after this file** stops pending work without claiming to cancel a
request already saving. Keep queue state in memory, warn before leaving, and
explain file reselection after reload/validation. **Start another batch** opens
a fresh page after completion.

Pasted text preserves browser-submitted content without trimming or normalization;
offer document upload for exact file bytes. Adding eligible evidence queues
bounded local checks; follow their status in Processing and results from Case
Tools or the evidence reader. Do not imply every file has a supported extraction
profile.

Vehicle-location CSV review leads with vehicle mapping and possible-duplicate
counts. Source-row preview is collapsed unless invalid rows need attention.
Completion links to Case Tools and discloses individual observations; the original
CSV and attribution remain accessible. See [imports](vehicle-location-imports.md).

## Case Tools, Processing, and additional analysis

Case Tools groups destinations under **Explore evidence**, **Organize your
work**, and **Reports**, side by side on wide screens and stacked on narrow
screens. The header links directly to **Evidence** and **Processing**, alongside
owner/editor **Add evidence**. Five report links stay together in the directory;
owner/editor **Additional analysis** is directly available with organizing tools.
Below the directory, previews show up to five recent findings and five recent
active records. Link full findings/reports rather than expanding measurements or
history. Recent findings retain attribution; linked readers/reports retain exact
output, reviews, coverage, and uncertainty.

Processing separates **In progress** from paginated **History**, which retains all
check outcomes and has evidence-type and outcome filters. Those filters do not
hide current work. Vehicle-location CSV imports retain their own progress and
review links; imports ready for review remain with current work. Preparation and
vehicle-import histories use separate native disclosures. Preparation batches stay
distinct from execution: **Checks queued** does not mean those checks have finished.
Owners and editors have **Run more checks → Rerun all analysis** here; it starts
supported local checks across active evidence, preserves earlier results, and
reports background preparation progress.

While work is active, Processing, analysis results, evidence findings/history,
message and vehicle import reviews, and vehicle trip summaries update their
changing sections every five seconds with JavaScript. The original reader,
navigation, filters, URL, and interactive map remain in place. Focused sections
and edited forms wait for the investigator; open disclosures stay open. Polling
pauses in hidden tabs and stops when work finishes. Requests time out after 15
seconds; three consecutive failures pause updates with a manual retry. Session or
access loss stops updates and offers All cases. **Refresh status** also works
without JavaScript. Every fragment request repeats the page's current case checks.

The prominent **Reprocess evidence** button opens a dedicated screen. Native
checkboxes select multiple evidence types with active counts; Local is the default
processing location, with External / AI and Both as alternatives. Explain local
extraction prerequisites, separate local/external AI runs, configured lookup
locations, retained originals/results and bounded comparison coverage. Keep
external-transfer approval visible beside the action. Invalid submissions retain
choices and link errors to their controls. Submit returns to the selected batch
in Processing; viewers have no reprocessing action. Methods needing a question,
explicit samples, area, event or import interpretation link to Choose analysis.
Confirmed message CSV imports automatically start message, writing, and local model
analysis. Their review page links to **Follow message analysis** in Processing;
completed or stopped preparation offers **Run message analysis again**. Each
result discloses its bounded message selection. Other additional AI and external
operations remain in the deliberate analysis flow.

**Case Tools → Additional analysis** and **Processing → Run more
checks → Choose analysis** group tasks, then open a source/context form. Keep
required samples, areas, and claimed events visible; disclose tuning in **Options**,
opening after errors. Keep versions explicit and require external approval.
Sources use 25-record pages; native POST navigation/title search preserve choices
and options. Show off-page selections with Remove. Browsing never queues work;
execution/inference limits still apply.

Use the shared field renderer for labels, help, errors, approvals, and reviews.
Progress uses a native indicator; failures expose retry/recovery controls. Results
lead with output and sources before processing metadata. Use retained image
regions/crops, recording intervals, paired passages, message sequences, and
observation maps when supplied by the operation. Identifier normalization lives
in **Value details**.

Reports lead with a summary and visible coverage warnings; **About this report**
holds methods/totals. Chronological/grouped sources use 25-record pages; totals
and comparisons cover the complete filtered selection. Participant pairs paginate
independently; thread/reply lists and vehicle observations retain complete paged
access. Structured lists/mappings use independent 200-value pages, preserving
filter/finding context and full downloads. Narrow rows stack with labels. Trips
show dates, endpoints, observed duration, and disclosed observations; omit repeated
vehicle names. Pagination does not imply exhaustive coverage or verified journeys.

## Collaboration and request behavior

Owners manage a compact owner/collaborator/pending-invitation table. Viewer is
the default invitation role; ownership is fixed. Role changes are explicit saves;
removal has a named confirmation and affects subsequent operations. Invitations
show pending, accepted, expired, revoked, and delivery-failed states honestly.
Preserve the invitation through registration/verification and offer wrong-account
recovery. Expired/revoked links explain how to contact the owner; failed mail
leaves a retryable pending invitation. See [cases](cases.md) for token lifetime,
rotation, verified-email matching, and acceptance/replay rules.

All mutations use CSRF-protected POST and current server-side case/object access.
Superusers receive no case bypass. Inaccessible objects return generic not-found
responses; prohibited viewer writes return permission-denied. Enhanced uploads
must distinguish structured authentication/access errors from successful JSON.
Use the same forms and authorization as ordinary submissions, with safe field
errors, idempotent retries, and no secret-bearing exception text.

## Accessibility and acceptance

Apply [DESIGN.md](../DESIGN.md#completion-and-design-review) for viewport,
keyboard, accessibility, and recovery checks. Use [validation](validation.md)
for check selection, executable commands, and synthetic capture locations.
