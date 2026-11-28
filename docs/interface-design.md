# Investigator interface specification

This guide owns screen behavior. See [DESIGN.md](../DESIGN.md) for shared
visual and accessibility standards, and [validation](validation.md) for checks.

## Navigation and screens

- Cases open to **Case Tools**. The labeled rail links directly to **Case Tools**,
  **Evidence**, and **Processing**. On narrow screens it becomes top navigation.
  **All cases** switches cases and filters accessible names and descriptions by
  ownership. Public and account workflows are covered in
  [marketing](marketing.md) and [cases](cases.md#account-settings).
- **Search this case** sits beside the case name and wraps below it at narrow
  widths. Ctrl/Cmd+K focuses it. Queries of two or more characters offer up to
  six quick matches. Arrow keys and Enter select a result, and Escape closes
  suggestions. **Search all results** submits the typed query. **Filter
  passages** narrows the ranked results by record type or date. See
  [Cloudflare search](cloudflare-search.md) for index coverage and availability.
- Evidence, Images, Findings, People, Vehicles, and POIs use hosted search
  for typed queries within their collections. Clear the query to browse with
  structured filters when hosted search is unavailable. All cases and the
  conversation transcript have their own local filters.
- Live collection filters apply after a 300 ms typing pause where supported.
  They update the URL, count, rows, and pagination, starting at page one.
  Filtering in a split Evidence workspace keeps the original open. Distinguish
  an empty collection, no matches, invalid filters, and unavailable search.
  Preserve the criteria and offer a clear or recovery action.
- Compatible links keep the shell mounted. Paging updates the affected list or
  report. Back restores the selection, filters, and scroll position. Short
  evidence metadata edits, people and vehicle forms, and new Important notes
  use dialogs with full-page fallbacks. Keep imports, approvals, and deletion
  on dedicated pages.

Chronology shows newer days above older ones. Keep date and clock controls
outside the day sections and offer ordinary links when scroll loading is
unavailable. Place each time beside its source, retaining both endpoints of
overnight trips. Both timeline views link to **Unplaced evidence and time
claims**, including on empty days. See [Chronology](chronology.md).

## Visual reference and density

The synthetic workspace reference illustrates the rail, collection, and reader.
Use [layout and density](../DESIGN.md#layout-and-density) for breakpoints and row
targets. At default zoom, target at most `164px` for the collection heading,
Add, search, and filters, and `44px` for pagination. Use about `65ch` for original
text. Let titles and larger text expand rows.

![Quiet evidence workspace](design/quiet-evidence-reference.png)

## Cases and investigative records

Creation asks for a name. **Case settings** leads with collaborators and
invitations, followed by **Rename case** and AI controls. Keep the destination,
transfer scope, approval requirements, and current decision visible. **Models
by task** shows the resolved defaults and supported effort choices. Stack
model fields at narrow widths and present indexing status in Processing.
Follow [cases and collaboration](cases.md) for roles, conflicts and deletion.

People and Vehicles use whole-row links. Their forms pair short fields when
space permits and give long fields and the linked error summary full width.
Keep **Delete person** and **Delete vehicle** below **Record details** for owners.
Vehicle headers place **Add a location** and **Import vehicle locations** beside
**Edit vehicle**. See [People](people.md) and [Vehicles](vehicles.md).

## Evidence browsing and reading

- Counts above the Evidence filters cover every type in the selected state
  (Active, Archived or All), including zero counts. Search, type, person,
  checksum and page do not change that scope.
- Wide collections show **Evidence**, **Type**, **Details**, and **Date and
  time** columns. In a split or narrow pane, stack details below the title.
  Show explicit person roles, archive labels and matched passages when
  available. Label each time by its role, show its timezone, and preserve
  date-only precision.
- Use real links and expose the selected row with `aria-current`. The selected
  original takes the page heading, with the collection as a lower-level
  heading. Keep search and structured filters in the URL. Keep **Apply filters**
  and **Clear filters** close to their fields.
- A reader header presents the title, source and collection line, archive state,
  and **Download original**. **Findings (count)** jumps past the original to
  its complete current findings, including timing-only items. Findings,
  **Analysis history**, and the Evidence list page independently.
- **Info** holds supporting provenance, integrity, saved descriptions and history.
  Give it a named keyboard focus stop for long content. For active evidence,
  owners and editors can **Edit details**, **Analyze evidence**, and **Mark
  important**. Analyze evidence links directly to identifier and date extraction
  for eligible originals. **All analysis methods** opens the compatible
  catalogue. **More** holds secondary actions, including archive or restore.
- Keep canonical readers and source downloads usable without JavaScript.
  Preview failure leaves metadata and download recovery available. See
  [original previews](original-previews.md) for format and page navigation.

Archiving keeps the selected reader open with the **Archived** label and
**Restore**, while removing the record from ordinary case work. Evidence
deletion shows the complete paginated inventory and full download before
confirmation. Cancel returns to the same context. See [evidence
retention and deletion](evidence.md#permanent-deletion).

## Conversation reading

Owners and editors have **Add text message** and **Import message CSV** in the
Conversations header. Show a wrapping sender and time line above each
selectable message preview. Preserve whitespace and link longer messages to
their full originals. Use the display timezone for date separators and retain
the reported calendar date when an original supplies a date alone.
A real message link opens the named details panel and **View in conversation**
returns to that message and page. Close or Escape returns focus to the message.

Show inferred session membership beyond color. Use the first and last cited
messages for boundary labels, even across transcript pages. Show conversation
findings when present and keep their pagination independent. Scroll loading
adds adjacent pages and preserves focus and position when prepending. Use
ordinary GET links for tabs, paging, expansion and recovery, deduplicate date
headings and keep the search terms and highlighted matches when paging. For a
search jump, bring the active highlighted phrase into view and let keyboard focus
continue to the match controls below the message. See
[reading conversations](evidence.md#reading-conversations).

## Evidence creation and batch uploads

Place mixed-file upload beside manual entry and imports on wide screens.
On narrow screens, stack the sections with **Upload files**, **Add a record**
and **Import CSV** jump links. In single-record forms, lead with original
fields and put **Change type** below the save row. Single-file Document is
the upload fallback.

Show configured limits. Pickers and drop append to the queue without replacing
pending edits. Each row shows filename, title, type, source overrides and state.
Open optional source details after validation errors. Shared details apply to
pending entries until overridden, including with a blank value. **Use shared
details** resets an override.

| State | Behavior |
| --- | --- |
| Ready | Edit or remove before sending. |
| Uploading or saving | Freeze submitted fields. Show upload progress, then saving until confirmed. |
| Added | Link to the retained record and keep it counted in the batch. |
| Failed | Preserve inputs. A confirmed validation failure permits correction and a fresh submission ID. |
| Outcome unknown | Retry the frozen submission ID and payload to resolve the outcome. |
| Access removed | Stop queued writes and explain recovery. |

**Stop after this file** pauses pending work. **Start another batch** clears the
queue after completion. Explain file reselection if leaving the page or reloading
would drop files. See [intake and duplicate review](evidence.md#investigator-workflow).

Vehicle imports lead with mapping, duplicates, and rows needing correction.
Completion links to the source CSV and, when observations were retained, to the
map. Message review uses one **Person** chooser per participant, with inline
creation. Call history upload asks for the file, then the source line and export
timezone. Its prepared review compares overlaps, supports individual exceptions,
and flags unresolved times. See the [vehicle](vehicle-location-imports.md),
[message](analysis-communications.md#message-csv-imports), and
[call](evidence.md#call-history-csv-import) workflows.

## Instagram monitors

Show each direction's changes, two linked captures, coverage, and schedule.
Keep the latest successful check, the next scheduled check, and connection
recovery easy to find. See [Instagram monitors](instagram-monitors.md).

## Case Tools, Processing, and additional analysis

### Case Tools and chat

**Explore evidence** links to Evidence chat, Findings, Conversations, Trips,
Images, Important, and Chronology. **Case records** links to People, Vehicles,
Repeated identifiers, Narrative relationships, Sighting groups, and eligible
Instagram monitors.
**Reports** links to each report. Display the groups in one row when space
permits. Owners and editors have **Add evidence** and direct method setup links.
**Latest records** interleaves up to three active originals and three saved
findings, newest first, with type and localized time.

Show the ten most recent saved chats beside the wide transcript, highlighting
the open chat. **All saved chats** opens the paginated list. Put investigator
questions in right-aligned bubbles with author, time, and source scope. Keep
reply text in an open reading column. Numbered citations open their sources,
with quotations available in the transcript and on the reply page. Keep the
follow-up composer below the transcript, using separate transcript scrolling
when space permits.

Pending replies show the worker step and available counts. **Review activity**
remains available after completion and full-review replies link to paginated
matches and coverage gaps. Marking a reply saves a link to the whole reply.
See [Evidence chat](analysis.md#evidence-chat) and [Important
items](cases.md#important-items).

### Processing and refresh

**Browse findings**, **Choose analysis**, and **Reprocess evidence** are direct
header actions, with setup and reprocessing limited to owners and editors.
The overview counts work across the case even when history is filtered. **Needs
attention** leads to required reviews and failures. **In progress** shows the
current stage and start time, using a determinate bar when a total is measured.
Show a few current failures per preparation workflow with a recovery link.
**View search indexing progress** retains focus and disclosure state when its
stage and counts update.

**Processing history** has visible evidence-type and outcome filters. Batch and
check links preserve the selected batch and history filters. **Analysis run
counts** reports saved outcomes across the case. **Checks queued** describes
completed preparation. See [worker
progress](background-tasks.md#investigator-progress-and-completed-imports).

Update progress and the case badge in place while the tab is visible. Fall back
from the event stream to status checks every 30 seconds while idle and five
seconds during active work. A visible pending chat checks every second with
backoff if the stream is unavailable. **Refresh status** serves the manual path,
including without JavaScript. Updates time out after 15 seconds. After three
consecutive failures, **Retry updates** resumes them.

Keep populated or filtered lists, selected originals, open forms and maps in
place. Use **Show updated results** in case collections and named refresh
links for findings and history, including histories populated only by call
imports. Keep a requested refresh pending through a failed status check so a
successful retry can apply it. The first unfiltered People or Vehicles record
can appear automatically if the empty collection is unused. A form or focus
inside the list defers that update. Access loss stops updates, clears private
contents, and offers **All cases**.

### Methods, findings and reports

Method setup carries the selected original and Evidence return context through
source selection, validation, cancellation and Processing. Analysis source
search, paging and removal update the picker without replacing task fields or
open options. When a draft edit interrupts picker navigation, announce the
interruption and leave the picker controls available to retry. Focus the error
summary if a report filter is invalid. Keep required evidence and questions
visible and put tuning under **Options**. Explain unavailable sources beside
their selection and link
preparation where applicable. All actions have ordinary form fallbacks.

Use explicit quotation selection for **Compare a claim**. **Compare plans with
locations** uses separate statement and observation pickers, quotes in context,
and reviews the interpreted subject, place and time before queuing. Comparison
results link the exact passage, observation or saved trip membership. Sighting
groups show the anchor, individual connections, supporting and contrary sources,
and saved reviews. See [plan and location
comparisons](analysis-locations.md#compare-plans-with-locations) and
[source-linked analysis](analysis.md).

- Lead run overviews with findings, citations and visible material limitations.
  Put selected sources, settings, history, downloads and **Run again** in the
  supporting disclosure. Preserve the destination and versioned source scope.
- Individual findings use one reading column with a contextual Back link and
  **Cited evidence**. Put quotation, original source link and provenance
  together, and show **AI suggestion** beside generated output. Keep coverage,
  review status, important warnings and failure reasons visible.
- Put offsets, native IDs and other secondary values under **Finding details**.
  Render relevant regions, recording intervals, paired passages and
  observations using their retained anchors. Show structured values when
  they form the result.
- Hide timing-only message pairs by default in run, catalogue, person,
  conversation and case highlight views. Show the hidden count and
  **Show all findings**. Preserve the reveal setting and direct finding URLs.
- Reports show totals, scope and warnings before records. Filters update
  results in place and preserve the selection in the URL. Disclose methods under
  **About this report**. Totals cover the full filtered scope. Paginate grouped
  and long structured lists independently and keep complete downloads available.

**Trips** keeps **Vehicle** and **Jump to date** visible above the saved trip
list and map. A date jump opens the first overlapping trip in the displayed
timezone. Trip history, coverage gaps and POIs paginate separately. See
[Trips and the investigation map](maps.md).

## Collaboration and request behavior

Invitation and membership screens name the person, role, state, and recovery
action. Keep acceptance after verification and show account-switching recovery
when the email differs. Save role changes explicitly and confirm removals by name.
See [cases](cases.md#invitations).

Private requests require current server-side access. Inaccessible objects use
generic not-found responses. Prohibited writes use permission-denied responses.
Enhanced uploads distinguish access errors from successful JSON, and reuse
normal validation and idempotent submission rules.

## Accessibility and acceptance

Follow the [design review](../DESIGN.md#completion-and-design-review) and
[interface review coverage](interface-review.md). Use synthetic records for
visual and browser checks.
