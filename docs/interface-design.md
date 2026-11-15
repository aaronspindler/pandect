# Investigator interface specification

**Status: core interfaces implemented; validation results are separate.**
This specification owns screen-specific behavior. [DESIGN.md](../DESIGN.md) owns
shared visual, accessibility, and interaction rules; [delivery and acceptance](interface-plan.md)
owns follow-up gates. Generated mockups are references, not rendered evidence.

## Navigation and screens

Cases open to **Evidence**, with **Findings** beside it. **Case tools** groups
Map, People, Vehicles, owner-only Settings, and Case details. Normal investigators
need no staff access. Graphs, a unified timeline, ownership transfer, and
self-service account deletion remain planned; show only implemented destinations.

All investigator screens share the Evidence navigation rail and shared controls,
including All cases and account settings. At narrow widths the rail becomes a
compact top bar. Case screens keep Evidence and Findings directly available;
**Case tools** opens Map, People, Vehicles, owner-only Settings, and Case details.
The current destination is marked; supporting pages also show a linked case name.
Disclosures work without JavaScript. With JavaScript, Escape/outside clicks close
the panel, and its Close control returns focus to the toggle.

Cases, People, Vehicles, and Evidence use shared whole-row links with readable
titles and secondary metadata. Detail pages and forms share the reader's heading,
action, divider, and spacing styles. Icon-only actions use shared hover/focus
tooltips, including Info/More when their words disappear on mobile. Tooltips can
be hovered and dismissed with Escape without moving focus. Native title tooltips
remain available without JavaScript. Decorative icons beside text need no tooltip.

| Screen | Content and primary action |
| --- | --- |
| All cases | Searchable owned/shared cases with roles; **Create case**. |
| Case Evidence | Active records, search and disclosed filters; **Add evidence** for owners/editors. |
| Evidence reader | Original and Findings together; downloads, Info, and authorized More actions beside the title. |
| Findings | Source-linked results and possible vehicle trips; reports and additional analysis on request. |
| People / Vehicles | Searchable case records; owner/editor creation and editing, viewer inspection. |
| Case settings | Owner-only name/access management and a separate delete section. |
| Case details | Owner, timestamps, role, and saved read-only description for all case members. |
| Invitation | Named recipient confirmation after authentication and email verification; **Accept invitation**. |
| Account | Allauth email/password management and POST sign-out, with the shared shell. |

The public `/` page serves marketing to anonymous visitors and sends signed-in
visitors to All cases. Product routes require an active authenticated account
with a verified email. Preserve safe local return destinations through sign-in
and verification. Email settings identify the current address/status and put
removal behind a named confirmation.

## Visual reference and density

The selected [quiet workspace reference](design/quiet-evidence-reference.png)
guides the evidence rail, collection, and focused reader. It is a synthetic mockup;
actual processing output takes precedence over illustrative findings. Earlier
references remain historical design context.

![Quiet evidence workspace](design/quiet-evidence-reference.png)

Use [DESIGN.md](../DESIGN.md#layout-and-density) for pane dimensions, responsive
breakpoints, row sizes, and visible-row targets. Keep the heading/Add action,
search, and Filters compact; target at most `164px` for this chrome and `44px`
for pagination at default zoom. Original text uses a comfortable reading measure
of about `65ch`; supporting metadata is grouped in the Info inspector.

Use 25 records per page and one whole-row link. Longer titles and larger text
can expand rows; never crop the accessible name to fit another row. Mobile
selection opens the canonical detail page with Back to evidence. At short
heights or high zoom, use ordinary document scrolling.

## Cases and investigative records

Case creation asks for a name and assigns the current user as owner on the
server. Search covers accessible case names/descriptions, with owned/shared
filters and stable newest-updated ordering. Empty cases offer the next action;
empty filtered results offer Clear filters. Only owners edit case metadata or
manage collaborators. Saved descriptions remain read-only.

People allow partial identities, requiring at least one name, phone number, or
email. Vehicles require at least a make, model, plate, or VIN. Owners/editors
create and edit; viewers inspect; owners alone delete through a named-target
confirmation. Case membership and login accounts are separate. Duplicate contact
or vehicle identifiers do not merge records or verify identity/ownership.

Person details include reported social accounts and POIs. Owners/editors manage
social accounts with confirmed removal; viewers read them. Search includes
platform/usernames. Saved notes and history are disclosed as read-only details.
See [people](people.md) and [vehicles](vehicles.md) for field and deletion rules,
including retained location links that can block individual deletion.

Case deletion is a separate owner confirmation naming the case and inventory,
including people, vehicles, active/archived evidence, collaborators, and pending
invitations. Require an acknowledgement and **Permanently delete case** POST.
Explain that accounts remain, there is no in-app undo, and storage/backups may
retain copies; do not promise verified erasure.

## Evidence browsing and reading

Use ordinary record links with `aria-current`, rather than a custom listbox.
The selected title is the page's single `h1`; the collection heading is lower
level. Each record has a canonical URL containing its case, allowlisted evidence
type, and UUID. Preserve query, type, archive state, and collection page when
moving between records or returning to the list.

Search stays visible and is capped at 200 characters. It searches metadata,
captured text/messages, supplied call transcripts, and supported completed
extraction text. Matching excerpts lead to the relevant original or retained
result; this is literal search, not semantic retrieval. See [analysis](analysis.md)
for extraction coverage. **Filters** contains type and Active/Archived/All,
with an explicit **Apply filters** submit. Non-default filters reopen on
navigation. Rows omit repeated ingestion dates; exact dates remain in history.

The reader has one header with the title, concise type/source/collection metadata,
archive state, and authorized download. Original content and source-linked
Findings appear together below it, without separate tabs or jump navigation.
Findings show coverage and status. **Run checks** appears beside empty findings;
reruns for populated findings are in More. Detailed reviews belong to individual results rather than a repeated “Awaiting review”
label on every highlight.

Header disclosures keep supporting material available:

- **Info** holds full source metadata, file details, saved descriptions, declared
  technical fields, record/integrity history, imported source context, retained
  attachment attribution, and processing history. History uses `results_page`
  independently of the collection's `page`; pagination keeps Info open.
- **More** holds permitted edits, archive/restore, additional analysis, and
  superuser deletion. Viewers can still open Info and download originals.

Use native disclosures with keyboard and no-JavaScript access. Validation errors
reveal their containing controls before focus moves to a field.

Show a private download for every case role. Escape text and never embed
untrusted documents as application HTML. Missing originals show a safe recovery
message while retaining access to metadata. Checksums have full selectable text
and an optional copy action. Editing details never changes original content;
a changed original is new evidence.

Archive/restore are explicit owner/editor POSTs. Archiving keeps the selected
detail visible, with Archived and **More → Restore**, while removing it from
the Active list. Current members can still inspect/download archived material. Individual
permanent deletion is reserved for superusers with current case access; show
originals, imported derivatives, analysis, and retained files in a complete,
paginated inventory with a full download. Bind confirmation to that inventory
and require another review if it changes. Cancellation preserves list context;
inconsistent cross-case links block deletion without exposing foreign records.
See [evidence](evidence.md) for the deletion and private-storage contract.

## Evidence creation and batch uploads

**Add evidence** opens mixed-file upload, with **Add text** and **Import vehicle
locations** alongside it. **More ways to add evidence** holds location and
communication forms. Single-record forms name their type in the title and offer
a labelled **Change type** disclosure with **Choose type** submit. All ten types
remain available without JavaScript. A single-file document form is the upload
fallback.

Required title/original fields stay visible. Optional source and declared
technical fields open on request, when editing, or after validation errors.
Saved freeform descriptions/notes remain readable; new note authoring is retired.
Source citation and finding review reasons remain available.

The default limits are 20 batch entries, 100 MiB per file, and 1 MiB UTF-8 captured
text; show configured operator limits. Each file is a separate request. A batch
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
bounded local checks and exposes status/results in Findings. Do not imply every
file has a supported extraction profile.

Vehicle-location CSV review leads with vehicle mapping and possible-duplicate
counts. Source-row preview is collapsed unless invalid rows need attention.
Completion links to Findings and discloses individual observations; the original
CSV and attribution remain accessible. See [imports](vehicle-location-imports.md).

## Findings and additional analysis

Owners and editors have a single **Rerun all analysis** button in the case Findings
header. It starts supported local checks across active evidence, preserves earlier
results, and reports background preparation progress. Additional AI and external
operations remain in the deliberate analysis flow.

Case Findings leads with useful results and possible vehicle trips. Source-linked
processing history is secondary, retaining evidence-type/outcome filters.
Additional analysis first groups tasks; selecting one opens its source/context
form. Required samples, areas, and claimed-event inputs stay visible. **Options**
holds optional tuning and opens after errors. Source versions remain explicit,
and external processing requires approval.

Use the shared field renderer for labels, help, errors, approvals, and reviews.
Progress uses a native indicator; failures expose retry/recovery controls. Results
lead with output and sources before processing metadata. Use retained image
regions/crops, recording intervals, paired passages, message sequences, and
observation maps when supplied by the operation. Identifier normalization lives
in **Value details**.

Reports put a concise summary before chronological/grouped source rows. Keep
coverage warnings visible; **About this report** holds fixed methods and totals.
Narrow rows stack with labels intact. Trip rows show dates, endpoints, observed
duration, and disclosed source observations; vehicle pages omit repeated names.
These display rules do not imply exhaustive coverage or verified journeys.

## Collaboration and request behavior

Owners manage a compact owner/collaborator/pending-invitation table. Viewer is
the default invitation role; ownership is fixed. Role changes are explicit saves;
removal has a named confirmation and affects subsequent operations. Invitations
show pending, accepted, expired, revoked, and delivery-failed states honestly.
Resend/role change rotates tokens; links expire after the configured period
(default seven days). See [cases](cases.md) for the complete acceptance contract.

Acceptance uses GET confirmation then POST from an authenticated account with a
matching verified email. Preserve the link through registration/verification and
offer wrong-account recovery. Expired/revoked links explain how to contact the
owner; failed mail leaves a retryable pending invitation. Replay cannot create
another membership or change its role.

All mutations use CSRF-protected POST and current server-side case/object access.
Superusers receive no case bypass. Inaccessible objects return generic not-found
responses; prohibited viewer writes return permission-denied. Enhanced uploads
must distinguish structured authentication/access errors from successful JSON.
Use the same forms and authorization as ordinary submissions, with safe field
errors, idempotent retries, and no secret-bearing exception text.

## Accessibility and acceptance

Apply [DESIGN.md](../DESIGN.md#completion-and-design-review) and the
[delivery gates](interface-plan.md#verification-gates). Core forms work without
JavaScript. At desktop widths label list/reader regions and make scrolling
keyboard reachable; at mobile widths preserve Back/filter context and avoid
horizontal page scrolling. Exercise empty states, missing fields/storage,
revoked access, stale edits, expired invitations, partial batches, and uncertain
confirmation. Use [browser checks](validation.md#browser-checks) for executable
commands and synthetic capture locations.
