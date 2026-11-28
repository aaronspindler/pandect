# Evidence models and original storage

Every evidence record belongs to one case. Domain apps own their evidence
models and readers. `core` owns the shared Evidence workspace, integrity and
storage, and cross-type deletion. See [app ownership](architecture.md).

## Reading and editing evidence

Readers keep originals beside their source-linked findings. **Findings (count)**
near the title jumps to every current finding citing the record or its attachments,
25 per page. **Info** holds provenance, integrity, saved descriptions and
a link to **Analysis history**. Owners and editors can **Edit details**,
**Analyze evidence** or **Mark important** on active evidence. **More** holds
removal from Important and archive or restore. See [marks and
notes](cases.md#important-items) and [Processing](analysis.md#retained-processing).

Text messages use a compact message excerpt, reported sender, and reported time
as their evidence-list label and reader heading. Times use the current display
timezone; missing times say **Time not recorded**. Excerpts take the first 80
source characters, collapse whitespace, and mark longer messages with an ellipsis.
The full original, saved title and import attribution remain available.

### Evidence packets

Owners and editors can open **Evidence → Prepare evidence packet** to choose up
to 12 active records across searches and pages. The picker shows the selected
count and lets you remove sources. **Prepare exhibits** makes one exhibit per
source. Use the original to check wording and record a page, message or time
reference. Number each exhibit, enter the disclosure copy, and remove sensitive
details as needed. Mark it as supporting, contrary or unresolved. The packet
chronology uses the dates you enter and puts undated exhibits last.

Preview the complete packet, then download the self-contained HTML. The file
opens offline. To print or save as PDF, turn off browser headers and footers
in the print dialog. The text-only packet contains the reviewed titles, roles,
dates, references and excerpts, plus any retained source checksums you select.
A retained-source checksum identifies the original and cannot verify a redacted
excerpt or interpretation.

Pandect does not save the packet as case evidence or send it to an external
service. Keep the editing page open while working. Download rechecks your access
and the availability of every selected source. Once downloaded, the file is
outside Pandect's access controls.

## Investigator workflow

Owners/editors open **Add evidence** to jump directly to uploads, manual records,
or CSV imports. **Upload files** accepts documents, images, and audio together.
**Add a record** offers Text, Location, Person location, Vehicle
location, Text message, Phone call, and FaceTime. **Import records** offers vehicle
locations, [call history CSVs](#call-history-csv-import), and message CSVs from
iMessage and WhatsApp. Message import retains its CSV, prepares a background
review, and requires confirmed people and timezone before publishing ready
messages from direct or group chats.
New evidence queues built-in checks and eligible checks using the
[case's selected AI backend](cases.md#external-ai-and-hosted-search) after publication. Follow
progress in **Processing**. See
[message imports](analysis-communications.md#message-csv-imports).

Categories describe submitted material; parsers do not verify them. Text captures
one browser-submitted original. Upload a text file as Document to preserve exact
bytes: textareas may normalize line endings, though the server never trims or
normalizes received text.
Add a single-message `.eml` export as a Document through either upload flow.
Pandect preserves the entire email file, including any embedded attachments, and
extracts selected headers and body text for search. Add relevant attachments as
separate evidence when you need to analyze them, with attribution to the email.
The extracted headers do not create case people or establish a sent time in the
chronology. See [email extraction](analysis-extraction.md#email-extraction).
If a person location or vehicle location has no matching case records to choose
from, the form explains the prerequisite and links to **Add person** or
**Add vehicle**. People and vehicles from another case never fill those choices.

Review titles, categories and provenance before uploading. Filenames seed
titles. Browser-declared image/audio types suggest categories, with Document
as the fallback. **Shared source details** applies until you edit an individual
field, including clearing it. **Use shared details** restores the shared value.
Collection times use the displayed timezone.

Batches send one file at a time. Saved records remain available if another file
fails. Correct known validation errors or retry the frozen submission when
confirmation was interrupted. **Stop after this file** pauses pending entries.
A reload may require file reselection. Use the single-document form without
JavaScript and **Change type** for other evidence types. See [batch states and
fallbacks](interface-design.md#evidence-creation-and-batch-uploads).

**Filter evidence** uses hosted ranked search to narrow the collection.
A matching transcript or finding can link to its passage. Clear the keyword
query to browse when hosted search is unavailable. Structured **Filters** for
type, related person, and active or archived state still work. Related person
matches explicit same-case links to person locations and communication parties.
Reader navigation retains filters and the collection paginates at 25. See
[Cloudflare search](cloudflare-search.md) for coverage.

The collection shows a title or message excerpt alongside the type and its most
useful recorded detail, such as a sender, filename, or linked vehicle. The date
and time field shows when a communication occurred, when a location was
observed, or when a file or text was collected. It retains the precision of
date-only message imports. The list still orders records by when they were added.

Use the [private readers](original-previews.md) to preview supported images,
documents, and WAV or AAC-in-M4A recordings. See that guide for format limits,
what a preview failure means, and how previews and downloads check integrity.

**Edit details** corrects metadata; stale drafts appear beside newer values
and require **Save reviewed changes**. Archiving removes an item from ordinary case
work and stops new processing that would use it. Open **Evidence → Filters → Archived**
to inspect or download the retained original. Owners and editors can choose
**More → Restore** to return it to active work. Archiving a source document leaves
previously imported rows in their own evidence state.
Individual deletion requires a superuser with current case access and [confirmation](#permanent-deletion);
ordinary investigators retain evidence until owner-confirmed whole-case deletion.

### Duplicate checks

Document, Image, Audio, and captured Text intake compares SHA-256 against active
file and text originals in the same case. Identical bytes match across
categories, filenames, and metadata. Captured text uses exact received UTF-8,
including whitespace/line endings, subject to browser normalization. This detects
exact copies, not similarity, re-encoding, revision, or meaning; it proves neither
source identity nor truth.

Matches pause before saving an original and show up to ten active records.
**Review all matches** opens the private, paginated checksum filter in a new tab
to preserve the queue. Remove the entry or **Add duplicate anyway** to retain
separate provenance. Other files continue, and later copies check earlier
saved entries.

Single forms support no-JavaScript review, preserving details but requiring file
reselection. Confirmation binds checksum, category, case, and uploader; changed
originals require renewed duplicate review. Confirmation rechecks edit access.
Retries resolve already-saved submissions before duplicate checks when the
original matches the saved checksum, including after interrupted confirmation.
Changed originals require a new submission.

These checks apply to single and mixed-file product intake for Document,
Image, Audio and Text.

## Models and metadata

[Shared models](../core/models/evidence.py) define common fields. Domain apps
own originals.

- Source/citation, URL, description, and collection time record provenance.
  Collection/upload times differ; saved descriptions remain readable/searchable.
- Checksums cover original bytes, captured UTF-8, or canonical location/communication
  JSON. They establish integrity, not authenticity.
- Archive attribution records the latest real transition, not a complete journal.
  Creation time is fixed; ordinary saves update edit time.
- Immutable submission IDs make product uploads idempotent within the case and
  uploading account. Trusted maintenance may omit them.
- At upload, Pandect retains the original filename's basename and computes the
  byte size and checksum. Its generated storage key is independent of the
  filename. Declared content type and technical metadata remain separate from
  extraction. Counts and dimensions must be positive and durations nonnegative.
  A zero duration is recorded as zero.

[Analysis](analysis.md) retains derived metadata, OCR, and transcripts separately.
Communication/location contracts follow; [vehicle imports](vehicle-location-imports.md)
own imported originals.

## Communications and retained call material

Manually added **Text message**, **Phone call**, and **FaceTime** records require
existing same-case sender and recipient, possibly the same person. Messages capture
one original, not a thread. Optional **Sent at/Call started at** records reported
event time separately from collection/upload. Calls accept optional duration such
as `00:01:30`: zero is valid, blank unknown. Time, duration, and identity are not inferred.

Forms/display use the labelled [account/device timezone](cases.md#account-settings);
stored instants/canonical downloads use UTC.

Participants, event time/duration, and message text are immutable. Display names
follow current person records; retained UUIDs preserve original, unverified
associations. Product/admin forms and normal validation/saves enforce same-case
participants. Either link protects individual person deletion, including archived
evidence; whole-case deletion removes both.

### Call history CSV import

From **Add evidence → Import records**, choose **Import call history CSV**.
Choose the CSV file and continue. For a CSV already retained in the case, use
**Import call history CSV** from its Document actions.

In source review, choose or add the line or account before preparing the calls.
Pandect defaults the export timezone to your account timezone, or
`America/Toronto` when the account has no timezone. Use **Change export
timezone** if the CSV uses a different zone. The source identifies which
exports came from the same line or account. Its case person link is optional.

Use a UTF-8 CSV with the fields `Call type`, `Date`, `Duration`, `Number`,
`Contact`, `Location`, and `Service`. The supported export reports
`Incoming` or `Outgoing`, a timestamp such as `2026-11-25 07:00:00`,
and a duration such as `00:01:30`. The import recognizes Phone, FaceTime
Audio, and FaceTime Video. It keeps source wording, including service
suffixes. The import accepts up to 20,000 rows and 16 MiB, subject to the
configured upload limit. Zero duration is retained as zero. The export supplies
no separate call outcome, and contact labels and numbers are reported information.

Pandect retains the exact original at upload. Background preparation builds a
paginated review. You can link case people in bulk by reported identifier or make an
exception for a specific row. Matching never creates a person. The review
flags possible repeats in distinct exports from the same source. Inspect both
sides, then keep or skip the flagged rows with individual exceptions. Both
originals and the skipped rows remain available for review. Rows with ambiguous
or nonexistent times stay in review with the original clock and reason while
ready calls publish.

Each published call appears in **Evidence** and **Chronology** and links to its
source row. Calls can appear with either or both people unlinked. Evidence
shows the source line or account and the reported identifier or **Unknown
caller** where applicable. A case owner or editor can choose **Link a person**
from the call's reader. The link records the investigator and time separately
from the source row and checksum. Case person filters use these explicit links.
Pair-based analysis requires two linked people.

### Reading conversations

**Case Tools → Conversations** groups direct messages by reported pair in either
direction, with 25 pairs per page. Open a pair for the chronological transcript,
50 messages per page, with unknown times last. Longer messages show excerpts
with a link to the full original. Scroll for adjacent pages or use the earlier
and later links without JavaScript or after a loading failure. If active
membership changes, reload the conversation before loading more messages.
Open imported group messages from the import result or **Evidence**. **Info**
shows the retained CSV, source row and sender details.

**Search messages** finds literal text in active message bodies across pages,
ignoring case and counting each matching timeline row once. It works with
external AI disabled. **Previous match** and **Next match** jump among highlights.
A long matching message shows the excerpt around its match.
**Clear search** keeps your place.

**Jump to date** opens the first message on that local day. An import that
supplies only a date uses its original calendar day and is labeled as having no
reported time. On **Sessions & summaries**, the jump opens the first saved
session with a message that day. If the day has no matches, choose another date.

The **Messages** tab opens by default and combines all exports and individually
retained messages. Matching copies share one timeline row when distinct exports
supply an unambiguous matching native message ID and the same original wording,
reported sender, recipient, and known time. Conflicting service identifiers,
matching IDs across WhatsApp and other export formats, and ambiguous repeats
remain separate. Use **Show retained copies** to inspect each original's export,
row and findings. Mixed-source transcripts include a short
source reference per message. The reported participants alone do not establish
native thread identity or a complete exchange.

Select a message to open **Message details** with the message text or an
excerpt, reported participants and time when supplied. **Open evidence** shows
the full retained original. Close the panel to return to it. **Message source**
shows the export and row or collection details. **View source metadata** opens
Info with native IDs, reactions, status and attachment references. Import
retains those references without fetching attachments.

Messages cited together in a saved inferred session share a colour.
**Session starts** and **Session ends** mark its first and last cited messages.
Expand **Inferred session** for the finding link and its boundary reason.
Uncoloured messages can fall outside the analyzed selection. **Multiple groupings**
means saved sessions disagree about membership. Expand it for the alternatives
and a link to the full paginated list when there are more than 25.
Missing messages and overlapping topics can affect any inferred boundary.

Switch to **Sessions & summaries** to browse saved groupings for the pair in
chronological order, 25 per page. Identical memberships appear once. Distinct
groupings can overlap. **Show messages** expands the saved messages in order,
50 at a time, with source references, bounded excerpts and links to the
originals. When a saved report covers the full session, a short AI suggestion
appears by default. Open **Full summary** for the saved statements and
**View cited report** for the complete report.
Owners and editors can **Request summary** for a session when an eligible
service has yet to summarize the exact grouping. **Summarize missing sessions**
opens a review of up to 50 saved sessions per page. Submit eligible groups from
that page and follow the batch in **Processing**. Owners and editors can use
**Group this conversation** at any time to review and regroup the pair’s active
messages. The new revision retains earlier interpretations. **Grouping details**
links delayed replies to the earlier messages that explain their inclusion.
Long sessions stay together across pages. **View summary coverage** shows which
ranges have a saved report or still need a summary. See
[communication analysis](analysis-communications.md) for the matching rules.

The message panel shows up to three **Findings** previews, and **More
findings** opens the complete paginated list. **Conversation findings (count)**
above the transcript shows session results and findings that cite several sources.
**Show message** opens the citation. Results label AI suggestions, partial
coverage and citations outside the conversation. The reader withholds content
when analyzed sources have changed or become unavailable. Reading never queues
analysis.

**Open evidence** shows original, findings, integrity, native reply/thread links,
and actions. **View in conversation** returns to that message with details open,
beyond page one, preserving the conversation-list page.

**Info → Imported source context** independently paginates native-thread messages
and reply candidates at 25 in source order, covering the complete same-case/export
population. Native IDs/multiple reply matches remain unverified references.

### Retained call material

Calls may contain no content, transcript, one audio file, or both, saved atomically.
Owners/editors later **Add transcript or audio** as a new `CallEvidenceAttachment`:
case, UUID, uploader, creation time, case- and uploader-scoped submission ID,
checksum, exactly one
phone-call/FaceTime parent, and transcript/audio/both. Attachments cannot be edited,
replaced, archived, or deleted individually. Corrections append material; earlier
content remains. Archived calls retain their additions. Restore the call before
appending more material. Viewers can inspect and download retained material.

Transcripts preserve received text without trimming, subject to browser line endings;
they are supplied material, not verification/automatic transcription. Requests
accept one file up to 1 GiB and supplied text up to 10 MiB UTF-8.
Calls use ordinary forms. Uncertain submissions retry the original ID/content;
new additions need new IDs. Failed file submissions may need reselection.
Opening the attachment form from filtered Evidence preserves that collection
through Back, Cancel, and save. Save returns to the new retained material.

**Download communication record** returns the hashed canonical UTF-8 JSON: sorted
keys, compact separators, unescaped Unicode, participant UUIDs, UTC ISO event time
or null, original message text or integer-microsecond duration or null. Schemas:
`pandect.text-message.v1`, `pandect.phone-call.v1`, `pandect.facetime.v1`. Adding
content never changes the original call checksum.

Each addition hashes `pandect.call-attachment.v1` JSON containing case/parent,
transcript, and optional audio metadata/byte checksum. **Info** downloads this
record; **Download transcript/audio** returns exact UTF-8/original bytes. Routes
recheck access/parent membership, use private/no-store and `nosniff`, and expose
no storage URLs. Collection search covers participants/contacts/audio filenames
without duplicate calls for multiple matching attachments.

The three communication models use scoped admin. Calls show read-only additions
without storage URLs. **View on site** opens the existing record's Evidence
reader. Attachments have no standalone admin. Case-admin deletion additionally needs
`phone.delete_callevidenceattachment` when attachments exist.

## Location observations

**Location** captures an address/coordinate pair; **Person location** adds an
existing same-case person. Both inherit source name/URL/description, collection
time, uploader, checksum, and archive attribution. Forms and normal validation/
saves reject foreign people.

Addresses retain up to 20,000 characters without trimming. WGS84 coordinates use
six decimal places: both latitude (-90–90) and longitude (-180–180), or neither.
A non-whitespace address or pair is required; zero is valid. Optional **Observed
at** records reported time separately from collection/upload. Unknowns remain
unknown; maps/analysis distinguish reported positions from estimates.

Address, coordinates, time, and person are immutable. **Edit details** corrects
metadata; corrections to observations need new records. Display names follow
current people, with UUIDs preserving the original unverified association.

**Download original** returns hashed canonical UTF-8 JSON: sorted keys, compact
separators, unescaped Unicode, six-place coordinate strings (positive zero), null
missing coordinates/times, UTC ISO observation time. Schemas are
`pandect.location.v1` and `pandect.person-location.v1` (adding `person_id`). Editable
metadata, including Source, is outside the checksum. Authorized viewers can read/
download archived observations.

New vehicle observations normalize signed zero speed to positive zero before
hashing to match PostgreSQL; existing originals/checksums are not rewritten.

Downloads use `application/octet-stream` and attachment filenames for consistent
browser saving, including Linux WebKit. Location/communication/attachment records
retain `.json` names/canonical bytes; text/transcripts retain `.txt`/UTF-8.

Person-location links protect individual person deletion even when archived;
whole-case deletion removes both. There is no shared concrete location table.
Individual evidence deletion follows the superuser workflow below.

## Permanent deletion

Superusers with current case access choose **More → Delete evidence** without
requiring staff status; admin additionally requires active staff. Ordinary staff/
model permissions are insufficient. Superusers cannot access/delete inaccessible cases.

Confirmation names the target, explains file/derivative removal, and requires a
checkbox plus CSRF-protected POST; GET never deletes. It works without JavaScript.
Cancel returns to the reader; success returns to Evidence with filters retained.
The inventory covers every affected source, derivative, import/review, analysis,
and file, with IDs/filenames/checksums, 50 entries per page, and complete private
JSON. Signed confirmation binds user/case/target/inventory; changes require review again.

Deletion removes original/call content, every selecting run (including attachment
sources), results, linked historical decisions, search text, files, and dependent analysis. Multi-source
runs disappear entirely; unrelated originals, people, vehicles, and analysis remain.
The inventory includes evidence chat turns that used a deleted source or
derived result, later follow-ups in that conversation, match rows, and related
Important marks and notes. Removing its first turn also removes the chat.
Source-document deletion also removes imported observations, messages, calls, batches,
staged rows, and context, including archived derivatives. Retained-run references
to removed prior runs are cleared and inventoried as updates.

`core.evidence.deletion` checks the full cascade before exposing private names and
again under the case lock. Inconsistent cross-case links block without exposing
foreign material. Product/admin share this service and record admin history.
Database deletion is atomic; unplanned ORM deletion still meets model `RESTRICT` links.

The case lock serializes deletion and analysis/import publication. Removed queued
jobs become no-ops; running jobs cannot recreate sources/publish derivatives.
Files are cleaned after commit, and rollback retains them. Larger review pages
can use the investigator's short-lived reviewed inventory while the case activity
revision remains current and no analysis or chat turn is running. Adding or
removing search outlines advances that revision. The worker expires these
review pages. The final confirmation always rebuilds the complete
inventory under the case lock. See [file cleanup recovery](#private-storage-and-deletion).
Deletion does not remove backups or external-provider retention.

## Django admin

In `/admin/`, find the model under its domain app, such as **Document** or
**Image**. The [shared case and role requirements](cases.md#django-admin) apply.
People, vehicles, import batches, and source documents must match the evidence
case. Admin/product exclude inconsistent maintenance-created records. Owner-only
case deletion counts all case evidence without exposing inconsistent source details.

Creation assigns the operator as uploader and queues the usual checks for the
new original. Forms share file/text limits and preserve
untrimmed originals/addresses. Existing originals, case, uploader, integrity,
imported values, and saved descriptions are read-only. Declared technical metadata
remains editable. **Is archived** records real transitions' operator/time; admin
history is not a versioned evidence journal.

Filenames expose no storage paths or URLs. **View on site** opens the evidence
reader for that record, which requires verified email. Individual deletion
requires a superuser with case access and [confirmation](#permanent-deletion). Admin confirmation
shows counts by record type, stored-file totals, and affected cases instead of
listing every item; the complete private JSON inventory is optional. Case-admin
deletion requires ownership/contained-model permissions and post-commit cleanup.

When Django `DEBUG=True` (`DJANGO_DEBUG=true`), evidence lists also offer
**Delete selected evidence (DEBUG only)** to superusers. Select records of one
evidence type, choose the action, review the summary, check the
confirmation box, and choose **Permanently delete selected evidence**. The action
supports selected rows or all records matching the current list filters, across
accessible cases. The summary shows selected evidence, total records, and stored
files per case, plus record-type totals across the selection. Confirmation and
the optional complete private JSON download work without JavaScript. The server
still rebuilds and verifies the complete inventory before deletion.
The same case-access, dependent-record, analysis, and post-commit file cleanup
safeguards apply. Changed selections or inventories require fresh confirmation;
the whole batch is atomic. Bulk deletion is unavailable with `DEBUG=False`,
including submissions from a previously opened confirmation page.

## Creating and editing records

Open `python manage.py shell` in the documented environment. This example assumes
the caller already resolved and authorized `case` and `actor`:

```python
from django.core.files.base import ContentFile
from document.models import DocumentEvidence
from text.models import TextEvidence

document = DocumentEvidence(
    case=case,
    uploaded_by=actor,
    title="Source statement",
    source_description="Synthetic contributor example",
    file=ContentFile(b"Synthetic source statement.\n", name="statement.txt"),
    declared_content_type="text/plain",
)
document.full_clean()
document.save()

text = TextEvidence(
    case=case,
    uploaded_by=actor,
    title="Original source excerpt",
    body="Exact original wording.\r\n",
    language="en",
)
text.full_clean()
text.save()
text.source = "Interview excerpt"
text.save(update_fields=["source"])
```

Creation calculates integrity, ignoring supplied checksum/size/filename. Files
must be nonempty named Django file objects, never storage paths/reused `FieldFile`.
Hashing reads 64 KiB chunks and restores position; text remains unchanged.
Trusted callers may retain duplicate originals; product intake requires review.
Call `full_clean()` for field validation: `save()` does not run blanket validation.
PostgreSQL constrains required content, relationships, checksums, and numeric bounds.

Normal validation/saves reject changes to case, uploader, creation time, original
text/file/location/person, filename, size, declared content type, checksum, and
submission ID. Changed originals require new records. Source names/descriptions,
collection dates, and optional type metadata may be corrected. Revoked collaborators'
evidence/attribution remains; uploader `PROTECT` blocks account deletion while retained.

UUIDs precede save. New instances always insert, never replace matching IDs even
with forced updates; saving deleted instances cannot recreate them.

Model `clean()`/`save()` enforce these safeguards. Queryset/bulk writes, raw SQL,
`FieldFile.save()`/`delete()`, and direct storage writes bypass some/all: treat them
as trusted maintenance, never bulk-ingest/replace originals. This is application
preservation, not storage write protection.

## Private storage and deletion

Django's default file storage uses `PANDECT_MEDIA_ROOT`. It defaults to the
checkout's `data/media` on the host and `/app/data/media` in Compose, where
`app_data` is mounted at `/app/data`. Generated keys follow
`evidence/cases/<case_id>/<model_name>/<uuid>` without filenames or extensions.
Operators may configure private `STORAGES`. The application supports storage
backends without a filesystem `.path`. See
[coordinated backups](operations.md#database-backup-and-restoration) for the
database and storage requirements.

Product upload/detail/download/archive/delete recheck current access and parent
case. Private readers also validate the stored key against its record's case and
file namespace before opening it. Attachments use `application/octet-stream`,
`nosniff`, and no-store, returning exact saved UTF-8 for text. Admin omits
storage URLs. Serve public static assets through WhiteNoise and keep media
directories and buckets private.
Exports/jobs must check access themselves; `uploaded_by` is attribution, not
permission, and ORM is a trusted backend boundary.

Trusted ORM deletion removes rows, while ordinary users have no individual path;
superusers use confirmed workflows. Case deletion cascades active/archived evidence.
File apps register `post_delete` through `core.storage.schedule_original_deletion`.
The deletion transaction checks the key and records it with its case, including
for queryset and cascade deletion. After commit, storage cleanup removes the
verified file and closes the pending record. Rollback retains rows and files.

Restore storage access before retrying pending cleanup. The command processes a
bounded number of committed keys and reports how many remain. Run it with the
same default storage configuration used when deleting the records.
Entries without verified case ownership, including pending entries from before
the case field was added, remain pending for operator reconciliation. Confirm
ownership and check for retained database references before removing any file.

```sh
docker compose exec web python manage.py retry_file_cleanup --dry-run
docker compose exec web python manage.py retry_file_cleanup --limit 1000
```

Failed new evidence or call-audio saves roll back the database before removing
the new file. Callers handle later outer-transaction rollback. Failed metadata
saves and uncertain commits retain files. Upload-side failures can leave
orphans. Keep recovery logs restricted and reconcile files against database
references before removing suspected orphans.

## Migration and backup

Domains own evidence tables; `core` owns processing. Use the [fresh baseline](architecture.md#primary-keys-and-the-initial-schema).
Back up PostgreSQL/private storage together before upgrades; database-only backups
omit files. See [operations](operations.md).

References: [Django abstract models](https://docs.djangoproject.com/en/6.1/topics/db/models/#abstract-base-classes),
[file storage](https://docs.djangoproject.com/en/6.1/topics/files/), and
[commit callbacks](https://docs.djangoproject.com/en/6.1/topics/db/transactions/#performing-actions-after-commit).
