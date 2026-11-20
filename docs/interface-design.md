# Investigator interface specification

**Status: core interfaces implemented; validation results are separate.**
This guide owns screen behavior; [DESIGN.md](../DESIGN.md) owns shared design and
accessibility rules; [validation](validation.md) owns checks and procedures.
Generated mockups are references, not rendered evidence.

Public pages, sign-in, and the investigator workspace share the graphite and teal
palette, system sans serif typography, heading weights, and control radius from
`ui/static/ui/primitives.css`. Compact eyebrow labels and technical values use
the shared system monospace stack. `ui/static/ui/app.css` owns workspace controls
and navigation; domain styles reuse the semantic tokens. Public layouts retain
larger introductory headings, while investigator screens retain compact reading
and collection layouts. Light, dark, and forced-color modes share the same
foundation across these surfaces.

## Navigation and screens

Cases open to **Case Tools**: recent evidence/findings and links to the Findings
catalogue, Map, Chronology, Conversations, Connections, People, Vehicles,
Additional analysis, and reports. Native **Case details** discloses supporting
information.
Owners reach **Case settings** directly from the page header. Investigators need
no staff access. Graphs, inferred
event timelines, ownership transfer, and self-service account deletion remain
planned; show only implemented destinations.

All investigator screens, including All cases and account settings, share the
labeled rail and controls. Case screens link directly to **Case Tools**, **Evidence**,
and **Processing**. The rail becomes a compact top bar on narrow screens. Mark
the current destination and link the case name on supporting pages. Case Tools
is a full page. Disclosures work without JavaScript.

**Search this case** sits beside the case name on each case screen and wraps
below it on narrow screens. Evidence keeps its case switch in this shared
header. Collection searches still filter their own lists. The case search is a
GET form. With JavaScript, Ctrl/Cmd+K focuses it and two or more characters
offer up to six quick matches. Use arrows and Enter to open a record, Escape
to dismiss the suggestions, or submit the typed query for full results. Quick
matches look up active records case-wide. The results page applies the submitted
filters.

Case search results link to the matching source, highlight literal terms, and
label dates and source information. **Filters and sorting** provides a
record type, local date range, evidence state and keyword ordering; the URL
retains the query, filters and page. **Related by meaning** shows up to six
additional source passages on the first page. An incomplete or unavailable
index is identified beside the keyword count, with a link to Processing.
**Search tips** describes phrases, alternatives and exclusions.

Cases, People, Vehicles, and Evidence use shared whole-row links with readable
titles and secondary metadata. Detail pages and forms share the reader's heading,
action, divider, and spacing styles. Info, More, and Filters retain their visible
labels on mobile. Edit details and Analyze evidence are directly available to
owners/editors in the reader. Analysis choices use a named disclosure; More keeps
secondary actions.

People and Vehicles retain list searches and pages through creation, editing,
deletion, and cancellation. Shared forms name choice groups with native legends;
error links focus the affected control or group.

| Screen | Content and primary action |
| --- | --- |
| All cases | Searchable owned/shared cases with roles; **Create case**. |
| Case Tools | Default case home; recent evidence and findings, supporting views, reports, and **Add evidence** for owners/editors. |
| Evidence | Active records, search and disclosed filters; **Add evidence** for owners/editors. |
| Processing | Active checks and imports, analysis run counts, paginated history, recovery links, and owner/editor **Rerun all analysis**. |
| Evidence reader | Original and Findings together; downloads, Info, and authorized More actions beside the title. |
| Conversations | Reported participant pairs, a unified message history, and saved instances with summaries and expandable originals. |
| Findings catalogue | Searchable retained source-linked results, with filters for evidence and method. |
| People / Vehicles | Searchable case records; owner/editor creation and editing, viewer inspection. |
| Case settings | Owner-only name/access management and a separate delete section. |
| Case details | Owner, timestamps, role, and saved read-only description for all case members. |
| Invitation | Named recipient confirmation after authentication and email verification; **Accept invitation**. |
| Account | Allauth email/password management, timezone preference, and POST sign-out, with the shared shell. |

Public `/` serves the same marketing overview to anonymous and signed-in visitors;
signed-in visitors have an **Open workspace** link to All cases. Public layouts
and content are owned by [marketing](marketing.md). Product routes require an
active authenticated account and verified email.
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

Use 25 records per page and one whole-row link. Let long titles and larger text
expand rows to keep the full name readable. When no record is selected, the
collection fills the workspace. Selecting a record on a wide, tall window keeps
the collection beside the reader. Narrow or short windows open the canonical
detail with Back to evidence and document scrolling.

Case Tools, Processing, and analysis pages use the available workspace width.
Case Tools reflows its groups as space permits, with reading passages and forms
following the shared measures.

## Cases and investigative records

Case creation asks for a name and assigns the current user as owner server-side.
Search accessible names/descriptions with owned/shared filters and stable
newest-updated ordering. Empty cases offer a next action; empty search offers
Clear filters. Only owners edit metadata or collaborators; saved descriptions
remain read-only.

Person details show reported social accounts and POIs. Owners/editors manage
accounts with confirmed removal; viewers inspect. Search includes platforms and
usernames. Disclose saved notes/history read-only. Person/vehicle deletion links
sit below Record details, outside its disclosure, for owners. Vehicle headers
expose Add a location and Import vehicle locations beside Edit vehicle; adding a
location preselects the same-case vehicle. [People](people.md) and
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
limitations and source links on individual results.

Header disclosures:

- **Info** holds full source metadata, file details, saved descriptions, declared
  technical fields, record/integrity history, imported source context, retained
  attachment attribution, and processing history. History uses `results_page`
  independently of the collection's `page`; pagination keeps Info open.
- **Analyze evidence** lists the analysis available for the selected original.
  **Edit details** is visible beside it.
- **More** holds archive/restore, reruns, supported imports, and superuser deletion.
  Viewers can still open Info and download originals.

The Info pane has a named keyboard focus stop so Page Down can scroll long
attribution, integrity, and history without moving the original reader.
Native disclosures support keyboards and no JavaScript. Reveal invalid controls
before focusing their fields.

PDF originals provide Previous/Next links and a labeled **Page number** with
**Go to page**. Both preserve collection filters; invalid page requests offer
**Open first page**. Unsupported audio uses a plain playback-unavailable message
with a download action.

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
line above the complete, selectable original. Keep sender names explicit and
participant alignment consistent; do not label either participant as the signed-in
user. Date separators and timestamps use the display timezone. Preserve message
whitespace and escape source content.

**Messages** is the default view. Combine imported and individually entered
messages in the pair's timeline. Keep short source references and archive labels.
Group confirmed matching copies from different exports in one row. **Retained
copies** gives access to each original's export, source row and details.

**Instances** lists saved inferred sessions in chronological order. Lead with
the saved AI summary when it covers the full instance. Display its date range,
message count and links to the report and grouping. **Show messages** opens
the original members inline, with separate paging for a long instance. When a
full summary is missing, owners and editors can **Request summary**.

Each message has a selectable link to **Message details**. On wide screens,
selection opens one named side panel and leaves the transcript in place.
On narrow screens, the details use the full reader width. Close or Escape
returns focus to the message. Real message URLs support Back, Forward and
JavaScript-disabled navigation.

Give all members of a saved inferred session the same tint. Label its first and
last cited messages, including single-message sessions. A page edge cannot
create a boundary. Keep uncited messages neutral. Show **Multiple
groupings** when different saved memberships cover the message, and link to
their findings in the panel. Selection, archive state and session boundaries
remain distinguishable without colour.

Message details show the original, reported participants, time and an archive
label when applicable. Use **Message source** for the retained export and row or
recorded collection details, and link to evidence Info for full import metadata.
Use **Inferred session** or **Multiple groupings** to disclose session context.
Start **Findings · N** open with up to three short previews. Link each title to
the full finding and retain applicable AI and partial coverage labels. **More
findings** opens the detailed list with 25 findings per page. Keep finding
pagination independent from message pagination. Above the
stream, a collapsed **Conversation findings · N** disclosure holds session and
multiple-source findings, with 25 findings per page and **Show message**
links to the cited messages. Show each shared finding once, explain citations
outside the participant pair, and retain complete source details through
**Open finding**. Do not repeat selected-message copies as analysis findings.

Use latest equivalent saved results. Mark individual model suggestions when a
run mixes AI and ordinary observations. Withhold finding content when analyzed
sources are unavailable or changed. An empty collection says **No saved findings**;
it does not establish complete analysis. Reading or expanding findings starts no
analysis and requires no JavaScript.

In the details panel, **Open evidence** opens the canonical reader and native
reply/thread lists. Its **View in conversation** returns to the exact message
and page, opens details, and marks the target beyond color. Preserve the
conversation-list page throughout.

Tabs, expansion, and evidence links use ordinary GET navigation. Scrolling near either
loaded transcript boundary adds the adjacent page in place, keeping the list context,
deduplicating date headings and retaining focus and position when prepending.
Announce loading, completion, and failures; retain ordinary earlier/later links
for keyboard use, without JavaScript, and as full-page recovery after a failed
request. Each request uses the same authorized, bounded page route. Browser Back
preserves the selected record and page. Use ordinary page scrolling at every
screen size. An empty Instances view links back to Messages.
See [evidence](evidence.md#reading-conversations) for interpretation limits.

## Evidence creation and batch uploads

**Add evidence** leads with mixed-file **Upload files**, then visible **Add a
record** links for text/locations/communications and **Import records** links for
vehicle locations and CSV messages. Single-record forms name the type and offer
**Change type → Choose type**. All ten types work without JavaScript; single-file
Document is the upload fallback.

Keep title/original fields visible. Disclose optional source/technical fields
on request, editing, or validation errors. Saved descriptions/notes remain
readable. New note authoring is retired. Preserve citations and legacy finding
review reasons.

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

## Instagram monitors

The optional [Instagram monitor pilot](instagram-monitors.md) shows each direction's
changes, two linked captures, coverage, and scheduled checks in a case. Personal
connection settings belong to the signed-in investigator.

## Case Tools, Processing, and additional analysis

Case Tools groups destinations under **Explore evidence**, **Organize your
work**, and **Reports**, side by side on wide screens and stacked on narrow
screens. The header links directly to **Evidence** and **Processing**, alongside
owner/editor **Add evidence**. Five report links stay together in the directory;
owner/editor **Additional analysis** is directly available with organizing tools.
Below the directory, previews show up to five recent findings and five recent
active records. Link full findings/reports rather than expanding measurements or
history. Recent findings retain attribution; linked readers/reports retain exact
output, sources, coverage, and uncertainty.

Processing separates **In progress** from paginated **History**, which retains all
check outcomes and has evidence-type and outcome filters. Those filters do not
hide current work. Between them, **Analysis runs** shows the case-wide total and
counts by method for retained runs with saved outcomes, including partial results,
no findings, insufficient evidence, and unsupported input. A new rerun counts
once it saves an outcome. Worker retries remain part of the same run. The five
most-run methods appear first, with additional methods in a disclosure. History
filters, pagination, and selected batches leave the case-wide counts intact.
Vehicle-location CSV imports retain their own progress and review links;
imports ready for review remain with current work. Preparation and
vehicle-import histories use separate native disclosures. Preparation batches stay
distinct from execution: **Checks queued** does not mean those checks have finished.
Owners and editors have a direct **Choose analysis** header link and
**Run more checks → Rerun all analysis** here; the latter starts
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
Message CSV import review offers Local checks (the default), AI, or Both for
automatic analysis of newly published messages. Explain that Local checks runs
no models, AI uses one model suite, and Both combines those checks and that suite
without duplicate local/external AI runs. Resolve AI to Gateway when fully
configured, otherwise local/private inference, and show that destination before
submission. Keep external-transfer approval beside the choice when Gateway is
selected. Retain the reviewed route through execution; runtime failure never
changes it. The completed review page shows the selected mode
and links to **Follow message analysis** on its dedicated status page. Submitting
**Run message analysis** opens that request's status page immediately, showing
preparation and only its checks, with live updates and a manual refresh fallback.
Preparation finishing means checks are queued, not that execution has finished.
Completed or stopped preparation offers **Run message analysis again** with the
same choice and fresh external approval. Each result discloses its bounded message
selection. Other additional AI and external operations remain in the deliberate
analysis flow.

**Case Tools → Additional analysis** and **Processing → Choose analysis** show
all available tasks in labeled groups, without collapsed categories, then open
a source/context form. Finding filters are open initially, with a visible
**Result history** choice and plain latest/all saved results labels. Keep
required samples, areas, and claimed events visible; disclose tuning in **Options**,
opening after errors. Keep versions explicit and require external approval.
Sources use 25-record pages; native POST navigation/title search preserve choices
and options. Show off-page selections with Remove. Browsing never queues work;
execution/inference limits still apply.

Saved results expose **Download results** beside the title. Limitations are open
initially, and the visible **Run again** section keeps any external approval and
destination explanation with its action. Technical method/settings remain under
Processing details. Findings lists lead with the useful preview and compact method
and coverage labels. Evidence-specific lists put distinct findings with a useful
next check first and show the reason beside the finding. Distinct search matches
remain under **Matching search details**.

Findings show the result and supporting content once. Use a closed **Details**
disclosure for secondary measurements, native references and exact message
anchors. Keep status, partial coverage, failure reasons, meaningful interpretation
and low-confidence warnings visible. Metadata inspections retain their useful
property tables, and findings without primary content retain structured values.
Communication pairs lead with a factual interval heading and compact messages:
reported participants, localized time with timezone, source link and body.

Hide timing-only message pairs by default across run results,
catalogue/person findings, conversations and highlights. Full collections show a
hidden count and **Show all findings**; an all-hidden view explains its state.
Keep shared-word candidates, native reply context and AI suggestions visible.
Reveal state survives navigation. Direct finding URLs always open the original
indexed item.

Use the shared field renderer for labels, help, errors, and processing approvals.
Progress uses a native indicator; failures expose retry/recovery controls. Results
lead with output and sources before processing metadata. Use retained image
regions/crops, recording intervals, paired passages, message sequences, and
observation maps when supplied by the operation. Identifier normalization lives
in **Details**.

Reports lead with a summary, visible coverage warnings and key **Report totals**
before the record list. **About this report** holds methods and additional
calculations/selection details. Chronological/grouped sources use 25-record pages; totals
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
