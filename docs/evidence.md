# Evidence models and original storage

`text`, `image`, `audio`, `document`, `geo`, and `phone` own concrete evidence
models, forms, readers, admin, processing, and tests. `core` owns the unified
workspace, analysis lifecycle, integrity/storage helpers, and cross-type deletion.
Every record requires exactly one `cases.Case`.

There is no global pool/shared concrete evidence table. `core.models.Evidence`,
`core.models.FileEvidence`, `geo.models.LocationContent`, and communication/call
bases in `phone.models` are abstract. Shared templates live in
`core/templates/core/evidence/`; readers use domain namespaces. `video` is a
registered placeholder without an evidence category.

## Reading and editing evidence

Readers keep originals beside source-linked findings. **Findings** near the
original's title jumps directly to its saved findings and check outcomes.
**Analysis outcomes**
links to checks and saved results that used the evidence. **Info** exposes
provenance, integrity and retained descriptions. **Edit details** and
**Analyze evidence** are directly visible to owners/editors.
**More** holds secondary actions such as archive/restore. **Processing** shows
current case work and opens case-wide history on demand. See the
[screen specifications](interface-design.md#evidence-browsing-and-reading)
and [automatic checks/recovery](analysis.md#retained-processing).

Text messages use a compact message excerpt, reported sender, and reported time
as their evidence-list label and reader heading. Times use the current display
timezone; missing times say **Time not recorded**. Excerpts take the first 80
source characters, collapse whitespace, and mark longer messages with an ellipsis.
The complete original, saved title, and imported source-row attribution remain
retained; these labels also apply to messages imported earlier.

## Investigator workflow

Owners/editors open **Add evidence** to jump directly to uploads, manual records,
or CSV imports. **Upload files** accepts documents, images, and audio together.
**Add a record** offers Text, Location, Person location, Vehicle
location, Text message, Phone call, and FaceTime. **Import records** offers vehicle
locations and iMessage CSV. Message import retains its CSV, prepares background
review, and requires confirmed people/timezone before publishing valid messages.
New evidence queues eligible checks, configured private inference and
[case-approved external analysis](cases.md#external-analysis-approval) after
publication. Follow progress in **Processing**. See
[message imports](analysis-communications.md#message-csv-imports).

Categories describe submitted material; parsers do not verify them. Text captures
one browser-submitted original. Upload a text file as Document to preserve exact
bytes: textareas may normalize line endings, though the server never trims or
normalizes received text.
If a person location or vehicle location has no matching case records to choose
from, the form explains the prerequisite and links to **Add person** or
**Add vehicle**. People and vehicles from another case never fill those choices.

Review titles, categories, and provenance before upload. Filenames seed titles;
browser-declared image/audio types suggest categories, otherwise Document.
**Shared source details** supplies source name, URL, description, and collection
time until an individual field is edited/cleared; **Use shared details** relinks
it. Saved/unconfirmed submissions retain submitted values.
Collection-time labels in shared and per-file details use the active display
timezone, matching how the server interprets those times.

The typed upload form always submits one file with the selected evidence type
and its own source details. The batch queue stays under **Add evidence**.
Without browser support/JavaScript, intake links to the single-document form.
**Change type** selects other records. Forms show configured limits. Saved entries
count toward the batch cap; **Start another batch** resets intake. See [configuration](development.md#configuration).

Batches send one file at a time with uploading, saving, added, failed, or uncertain
confirmation states. Successful records remain saved. Known validation failures
allow correction; interrupted confirmations must retry the same frozen submission
to avoid duplication. **Stop after this file** pauses pending entries. Queues live
in page memory, may require reselection after reload, and cannot resume uploads.
The [interface specification](interface-design.md#evidence-creation-and-batch-uploads)
owns detailed states, shared fields, keyboard access, and fallbacks.
Validation errors name the affected field and open its source details so the
correction is visible. A failed file can be retried independently; successful
files stay saved.

**Filter evidence** matches titles, descriptions, source details/URLs, filenames, location
addresses, and linked people/contacts, with type, explicit person, and Active/
Archived/All filters. Person filters include person locations and communication
sender/recipient links with role labels, never inferred name/contact matches.
Invalid/foreign person filters are rejected. Reader navigation retains filters;
collections paginate at 25. The record title opens the original reader. Matches
in extracted findings or supplied transcripts include a separate link to the
matching passage. Text originals display escaped and read-only.

Image previews support JPEG (`.jpg`/`.jpeg`) and single-frame PNG up to 16 MiB/
50 million pixels, including 48 MP iPhone RAW exports saved as JPEG. Multi-picture
JPEGs preview only the primary image; originals retain auxiliary pictures. The
bounded parser respects EXIF orientation, flattens transparency onto white, and
re-encodes JPEG at up to 2048 pixels per side without source metadata. Originals/
checksums stay unchanged. Unsupported, corrupt, oversized, changed, or unavailable
files retain downloads. Archived images and no-JavaScript previews work; JavaScript
adds loading/failure messages.

Preview requests recheck case access/checksums, use private/no-store and `nosniff`,
and process locally without AI credentials, external services, or public storage
URLs. They store neither previews nor analysis. Downloads use authorized attachment
routes. Supported documents/WAV recordings also have [private readers](original-previews.md).

Duplicate warnings show a preview and **Review all matches**, a private paginated
checksum-filtered list including archived originals, without resubmitting uploads.

**Edit details** corrects metadata; stale drafts appear beside newer values
and require **Save reviewed changes**. **Archive/Restore** retains original access.
Individual deletion requires a superuser with current case access and [confirmation](#permanent-deletion);
ordinary investigators retain evidence until owner-confirmed whole-case deletion.

### Duplicate checks

Document, Image, Audio, and captured Text intake compares SHA-256 against same-case
file/text originals, including archived records. Identical bytes match across
categories, filenames, and metadata. Captured text uses exact received UTF-8,
including whitespace/line endings, subject to browser normalization. This detects
exact copies, not similarity, re-encoding, revision, or meaning; it proves neither
source identity nor truth.

Matches pause before saving records/permanent files and show up to 10 originals
with category, archive state, and private links. Review opens a new tab to preserve
the queue. Remove the entry or **Add duplicate anyway** for separate provenance.
Other files continue; later copies check earlier saved entries. Provenance remains
editable. Nothing is automatically merged/deleted.

Single forms support no-JavaScript review, preserving details but requiring file
reselection. Confirmation binds checksum, category, case, and uploader; changed
originals require renewed duplicate review. Confirmation rechecks edit access.
Retries resolve already-saved submissions before duplicate checks, including after
interrupted confirmation.

Checks cover single/mixed-file intake only. Locations, communications, call
attachments, CSV imports, admin, and trusted model callers keep existing behavior.
Comparison uses retained-original checksums.

## Models and metadata

[Shared models](../core/models/evidence.py) define common fields; domain apps own
originals. Key distinctions:

- Source/citation, URL, description, and collection time record provenance.
  Collection/upload times differ; saved descriptions remain readable/searchable.
- Checksums cover original bytes, captured UTF-8, or canonical location/communication
  JSON. They establish integrity, not authenticity.
- Archive attribution records the latest real transition, not a complete journal.
  Creation time is fixed; ordinary saves update edit time.
- Unique immutable submission IDs make product uploads idempotent; existing records/
  trusted maintenance may omit them.
- Filenames/sizes are generated. Declared content type/technical metadata are claims,
  separate from extraction. Unknown numeric values remain unknown; counts/dimensions
  must be positive and durations nonnegative. Zero duration differs from unknown.

[Analysis](analysis.md) retains derived metadata, OCR, and transcripts separately.
Communication/location contracts follow; [vehicle imports](vehicle-location-imports.md)
own imported originals.

## Communications and retained call material

**Text message**, **Phone call**, and **FaceTime** require existing same-case sender/
recipient, possibly the same person; add people first if needed. Messages capture
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

### Reading conversations

**Case Tools → Conversations** groups messages by reported person pair regardless
of direction, showing count/latest time. Scrolling loads 25 pairs or 50 messages
at a time, oldest first. Linked-message readers can load earlier messages upward
without moving the current position. Unknown times sort last; UUID ties imply no
event order. Earlier/later links work by keyboard, without JavaScript, or after
loading failure. Every batch checks current access.

Use **Jump to date** on either tab to go to the first recorded message on that
day in the reader's display timezone. In **Sessions & summaries**, Pandect opens
the first saved session with a message on that day. Scroll up for earlier entries or down
for later ones. If the day has no matches, choose another date from the same
reader page.

**Messages** combines all exports and individually retained messages. Matching
copies share one timeline row when distinct exports supply an unambiguous
matching native message ID and the same original wording, reported sender,
recipient, and known time. Conflicting service identifiers and ambiguous repeats
remain separate. Expand **Retained copies** to inspect each original's export,
row, archive state and findings. Mixed-source transcripts include a short source
reference per message.
The reported participants alone do not establish native thread identity or a
complete exchange.

Select a message to open **Message details** beside the transcript. On a narrow
screen the details fill the reader. Close the panel to return to the message.
The panel starts with the original, reported participants and time, and marks
archived messages. Expand **Message source** to find the retained export and row
or recorded collection information. **View source metadata** opens evidence Info
with native references and source-supplied fields such as reactions, status and
attachment references. The attachments themselves are never fetched. With
JavaScript off, the same message link opens the details through ordinary navigation.

Messages cited together in a saved inferred session share a colour.
**Session starts** and **Session ends** mark its first and last cited messages.
Expand **Inferred session** for the finding link and its boundary reason.
Uncoloured messages can fall outside the analyzed selection. **Multiple groupings**
means saved sessions disagree about membership. Expand it for the alternatives
and a link to the full paginated list when there are more than 25.
Missing messages and overlapping topics can affect any inferred boundary.

Switch to **Sessions & summaries** to browse saved groupings for the pair, 25 per page.
Identical memberships appear once. Distinct groupings can overlap. **Show
messages** expands the saved originals in order, 50 at a time, including their
source references and links to Message details. When a saved report covers the
entire instance, a short AI suggestion appears by default. Expand **Full
summary** to read all saved statements inline and reach the cited report.
Long summaries load in the instance when you expand them.
Owners and editors can **Request summary** for available services without a saved
summary of that exact grouping.

**Findings** starts open with up to three short previews of saved findings for
the message. Select a finding title for its full details. **More findings** opens
the complete list at that message, 25 per page.
**Conversation findings** above the stream shows up to three saved session
results and findings citing multiple sources, with links to cited messages.
**More findings** opens the rest with separate pagination.
Findings follow the participant pair through their exact citations. Results may also cite sources outside the
current view and say so. **Open finding** retains full context, source inventory,
and history. AI suggestions and partial coverage remain labelled. If an analyzed
source has changed or is unavailable, the reader withholds the finding content.
Opening the reader or selecting a message never starts analysis.

**Open evidence** shows original, findings, integrity, native reply/thread links,
and actions. **View in conversation** returns to that message with details open,
beyond page one, preserving the conversation-list page.

**Info → Imported source context** independently paginates native-thread messages
and reply candidates at 25 in source order, covering the complete same-case/export
population. Native IDs/multiple reply matches remain unverified references.

### Retained call material

Calls may contain no content, transcript, one audio file, or both, saved atomically.
Owners/editors later **Add transcript or audio** as a new `CallEvidenceAttachment`:
case, UUID, uploader, creation time, unique submission ID, checksum, exactly one
phone-call/FaceTime parent, and transcript/audio/both. Attachments cannot be edited,
replaced, archived, or deleted individually. Corrections append material; earlier
content remains. Archived calls retain additions and permit more. Viewers inspect/download.

Transcripts preserve received text without trimming, subject to browser line endings;
they are supplied material, not verification/automatic transcription. Requests
accept one file up to 1 GiB and supplied text up to 10 MiB UTF-8.
Calls use ordinary forms. Uncertain submissions retry the original ID/content;
new additions need new IDs. Failed file submissions may need reselection.

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
without storage URLs; **View on site** opens product intake. Attachments have no
standalone admin. Case-admin deletion additionally needs
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
Source-document deletion also removes imported observations/messages, batches,
staged rows, and context, including archived derivatives. Retained-run references
to removed prior runs are cleared and inventoried as updates.

`core.evidence.deletion` checks the full cascade before exposing private names and
again under the case lock. Inconsistent cross-case links block without exposing
foreign material. Product/admin share this service and record admin history.
Database deletion is atomic; unplanned ORM deletion still meets model `RESTRICT` links.

The case lock serializes deletion and analysis/import publication. Removed queued
jobs become no-ops; running jobs cannot recreate sources/publish derivatives.
Files are cleaned after commit; rollback retains them. Failures need [manual recovery](#private-storage-and-deletion).
Deletion does not remove backups or external-provider retention.

## Django admin

Concrete models appear under `/admin/` → **Evidence** with [shared gates](cases.md#django-admin).
People, vehicles, import batches, and source documents must match the evidence
case. Admin/product exclude inconsistent maintenance-created records. Owner-only
case deletion counts all case evidence without exposing inconsistent source details.

Creation assigns the operator as uploader. Forms share file/text limits and preserve
untrimmed originals/addresses. Existing originals, case, uploader, integrity,
imported values, and saved descriptions are read-only. Declared technical metadata
remains editable. **Is archived** records real transitions' operator/time; admin
history is not a versioned evidence journal.

Filenames expose no storage paths/URLs. **View on site** opens product readers/
downloads, additionally requiring verified email. Individual deletion requires a
superuser with case access and [confirmation](#permanent-deletion). Admin confirmation
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

Django default storage uses local `PANDECT_MEDIA_ROOT` (`data/media`) or Compose
`/app/data` in `app_data`. Generated keys follow
`evidence/cases/<case_id>/<model_name>/<uuid>` without filenames/extensions. Code
requires no filesystem-only `.path`; operators may configure private `STORAGES`.
No hosted-storage dependency/credentials are installed.

Product upload/detail/download/archive/delete recheck current access and parent
case. Attachments use `application/octet-stream`, `nosniff`, and no-store, returning
exact saved UTF-8 for text. Admin exposes no storage URLs; public media routes
are absent. Never expose media directories/buckets. WhiteNoise serves assets only.
Exports/jobs must check access themselves; `uploaded_by` is attribution, not
permission, and ORM is a trusted backend boundary.

Trusted ORM deletion removes rows, while ordinary users have no individual path;
superusers use confirmed workflows. Case deletion cascades active/archived evidence.
File apps register `post_delete` through `core.storage.schedule_original_deletion`,
using storage APIs after commit even for queryset/cascade deletion. Rollback retains
rows/files; text/location originals disappear with rows. Other cases' files remain.

Storage failure neither undoes committed deletion nor stops other cleanup.
`core.storage` logs generated keys without filenames, content, or backend exception
text. There is no retry queue. Restore storage, then retry the exact logged key
in the application's management shell:

```python
from django.core.files.storage import default_storage

default_storage.delete(
    "evidence/cases/CASE_UUID/documentevidence/GENERATED_KEY_FROM_LOG"
)
```

Database/storage writes are not atomic. Failed new evidence/call-audio saves roll
back the database before removing the new file. Failed metadata saves retain
originals; cleanup failure preserves the save error and logs its key. Callers own
later outer-transaction rollback (CSV import handles its own). Uncertain commit
outcomes retain files because rows may have committed. These cases/crashes can
leave orphans or interrupt cleanup. Retain restricted recovery logs and reconcile
storage against database references before removing suspected orphans. An upload/
transaction error alone never justifies file deletion.

## Migration and backup

Domains own evidence tables; `core` owns processing. Use the [fresh baseline](architecture.md#primary-keys-and-the-initial-schema).
Back up PostgreSQL/private storage together before upgrades; database-only backups
omit files. See [operations](operations.md).

References: [Django abstract models](https://docs.djangoproject.com/en/6.1/topics/db/models/#abstract-base-classes),
[file storage](https://docs.djangoproject.com/en/6.1/topics/files/), and
[commit callbacks](https://docs.djangoproject.com/en/6.1/topics/db/transactions/#performing-actions-after-commit).
