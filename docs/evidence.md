# Evidence models and original storage

The `text`, `image`, `audio`, `document`, `geo`, and `phone` apps own their
concrete evidence models, forms, readers, administration, processing, and tests.
`core` owns the unified evidence workspace, shared analysis lifecycle, integrity
and storage helpers, and cross-type deletion. Each evidence record belongs to
exactly one `cases.Case` through a required foreign key.

There is no global evidence pool or concrete shared evidence table.
`core.models.Evidence` and `core.models.FileEvidence` are abstract bases;
`geo.models.LocationContent` and the communication/call bases in `phone.models`
are abstract too. Shared screens use `core/templates/core/evidence/`, while
original readers live in each domain app's namespaced template directory.
The registered `video` app is a placeholder and adds no evidence category yet.

## Reading and editing evidence

The reader keeps originals and source-linked findings together. **Info** exposes
provenance, integrity, retained descriptions, and processing history; **More**
contains permitted record actions. Case-wide history is in **Processing**.
See the [screen specification](interface-design.md#evidence-browsing-and-reading)
for layout and navigation, and [analysis](analysis.md#retained-processing) for
automatic checks, progress, and recovery.

## Investigator workflow

Inside a case, owners and editors choose **Add evidence** to select or drop
documents, images, and audio together under **Upload files**. The
**Add a record** section links to Text, Location, Person location, Vehicle
location, Text message, Phone call, and FaceTime forms. **Import records** links
to **Import vehicle locations** and **Import messages · iMazing CSV**. The
message importer retains the CSV, prepares a background review, and requires
confirmed people and timezone before creating valid text-message rows. See
[iMazing imports](analysis-communications.md#imazing-csv-message-imports).

File categories describe the submitted material; no parser verifies them. Text
captures one browser-submitted original; to preserve a text file's exact bytes,
upload the file as a Document instead. Browser textareas may normalize line
endings, but the server does not trim or normalize received original text.

Review titles, suggested categories, and source details before uploading.
Filenames supply initial titles; browser-declared image/audio types suggest a
category, with Document as the fallback. **Shared source details** applies a
source name, URL, description, and collection time to pending files until a field
is individually edited or cleared. **Use shared details** restores that link.
Saved and unconfirmed submissions keep their submitted details unchanged.

When browser support or JavaScript is unavailable, Add evidence links to an
explicit single-document form; **Change type** selects other record types.
Configured limits appear in the form. Saved entries count toward the batch limit;
use **Start another batch** after completing it. See
[configuration](development.md#configuration) for operator settings.

Batch uploads send one file at a time and show uploading, saving, added, failure,
and uncertain-confirmation states. Successful entries remain saved. A known
validation rejection allows corrections; an interrupted confirmation must first
retry the same frozen submission so an already-saved original is not duplicated.
**Stop after this file** pauses pending entries. Queues live only in page memory;
reloading may require file reselection, and uploads are not resumable.
The [batch interface specification](interface-design.md#evidence-creation-and-batch-uploads)
owns control states, shared-field behavior, and keyboard/fallback requirements.

The Evidence collection searches title, descriptions, source name/details/URL,
original filenames, location addresses, and linked person names/contact details,
with type, explicit related-person, and Active/Archived/All filters. A person
filter includes person-location observations and communications where that person
is sender or recipient, with role labels. It never infers a link from a matching
name or contact. Invalid or foreign-case person filters are rejected. Filters
remain in reader navigation. It uses 25 records per page. Text originals are shown
as escaped read-only text. Image evidence shows a resized
preview in the Original section, including for archived images. Previews support
JPEG (`.jpg` and `.jpeg`) and single-frame PNG files up to 16 MiB and 50 million
pixels, including 48 MP iPhone RAW exports saved as JPEG. Multi-picture JPEGs
display only the primary image; auxiliary pictures are retained in the original.
Previews are decoded in the existing resource-bounded child process, respect EXIF orientation,
flatten transparency onto white, and are re-encoded as JPEGs up to 2048 pixels per
side with embedded source metadata removed. The retained file and checksum are
unchanged. Unsupported, corrupt, oversized, changed, or unavailable files keep the
original-download action.
Previews also display without JavaScript; JavaScript adds loading/failure messages.
Preview requests recheck current case access, verify the retained bytes against
their checksum, and use private/no-store caching and `nosniff`. They use local
processing without AI credentials, external services, or publicly exposed storage
URLs. No preview files or analysis results are stored. Files are downloaded through
currently authorized attachment routes. Supported PDF/text documents and WAV
recordings also have [private in-product readers](original-previews.md).

Duplicate-upload warnings show a short preview and link to **Review all matches**.
That private Evidence list uses a retained-checksum filter, includes archived
records, and paginates every matching original without resubmitting the upload.

Owners/editors choose **More → Edit details** to correct metadata. Stale edits
preserve the draft beside newer values and require **Save reviewed changes**.
**Archive**/**Restore** organize evidence without removing original access.
Individual deletion is reserved for superusers with current case access through
the [confirmed deletion workflow](#permanent-deletion); ordinary investigators
retain evidence until owner-confirmed whole-case deletion.

### Duplicate checks

Adding a Document, Image, Audio, or captured Text automatically checks its SHA-256
against file and captured-text originals already in the same case, including
archived records. Identical bytes match across file categories, filenames, and
editable source details. Captured text matches its exact server-received UTF-8
bytes, including whitespace and line endings; browser normalization still applies.
This detects exact copies, not similar images, re-encoded recordings, revised
documents, or matching meaning. It does not establish source identity or truth.

A match pauses that entry before saving a record or permanent original and shows
up to 10 existing records, with category, archive state, and private detail links.
Review links open in a new tab to preserve the queue. Remove the file from the
queue or choose **Add duplicate anyway** when separate provenance warrants another
retained record. Other queued files can continue; later copies in the same batch
are checked against earlier saved entries. Source details remain editable during
review. There is no automatic merging or deletion.

Single-record forms also support review without JavaScript. Entered details remain
in the form, but a file must be selected again to confirm. The server binds the
confirmation to the original checksum, category, case, and current uploader;
changing the original requires another review if it matches existing evidence.
Current edit permission is rechecked on confirmation. Retrying an already-saved
submission resolves that record before checking duplicates, including after an
interrupted confirmation.

This check applies to single-record and mixed-file evidence intake.
Location/communication records, call attachments, CSV imports, Django admin, and
trusted model callers retain their existing behavior. Duplicate checks compare
the checksums of retained originals.

## Models and metadata

The [shared evidence models](../core/models/evidence.py) own common field
definitions; concrete originals belong to the domain apps listed above. Keep
these distinctions when reading or extending them:

- Source name/citation, URL, description, and collection time describe provenance.
  Collection time is separate from upload time. Saved investigator descriptions
  remain readable and searchable.
- Checksums are generated from original bytes, captured UTF-8 text, or canonical
  location/communication JSON. They establish integrity, not authenticity.
- Archive attribution records the latest real archive/restore transition, not a
  complete history journal. Creation time remains fixed; ordinary saves update
  the edit timestamp.
- An immutable, unique submission ID makes product uploads idempotent. Existing
  records and trusted maintenance callers may leave it unset.
- Original filenames and byte sizes are generated. Declared content type and
  optional technical metadata are supplied claims, not verified extraction.
  Unknown numeric metadata remains unknown; supplied counts/dimensions must be
  positive and durations nonnegative. A zero duration is distinct from unknown.

[Case analysis](analysis.md) retains derived metadata, OCR, and transcripts
separately from originals and supplied metadata. Communication and location
originals have the specific contracts below; vehicle import originals are
documented in [Vehicle locations and CSV imports](vehicle-location-imports.md).

## Communications and retained call material

Choose **Text message**, **Phone call**, or **FaceTime** inside the case's Add
evidence workflow. Sender and recipient are required existing people in that
case; they may be the same person. Add people first when the case has none.
A message captures exactly one original message, not a conversation thread.
Optional **Sent at** or **Call started at** records reported event
time, separately from source collection and upload time. Calls also accept an
optional reported duration such as `00:01:30`; zero is valid and blank is unknown.
No time, duration, or identity is inferred.

Date/time fields and displayed timestamps use your [account/device timezone](cases.md#account-settings),
shown in the field label. Stored instants and canonical event timestamps in
original downloads remain UTC.

Participants, reported time/duration, and message text are immutable originals.
Names shown in the reader follow current person records, while retained UUIDs
identify the original associations. These associations do not verify identity.
Both participants are restricted to the same case by product/admin forms and
normal model validation/saves. Either participant link prevents individual person
deletion, including for archived evidence; whole-case deletion removes both.

### Reading conversations

**Case Tools → Conversations** groups retained Text messages by their reported
pair of case people, regardless of sending direction. The list shows message
counts and the latest recorded time, with 25 pairs per page. Open a pair to read
50 messages per page in recorded-time order, oldest first. Unknown times appear
last; UUID order breaks equal or unknown-time ties without implying an event order.

The default **All sources** includes imported and individually entered messages.
Choose a retained export to narrow the reader to that source. Archived messages
remain readable and labelled; separate records from overlapping exports remain
separate. This grouping is a reading aid, not a verified native thread or proof
of identity, complete history, or response. Calls and inferred analysis sessions
remain separate views.

Expand **Message details** beneath a message for reported participants/time,
source attribution, and available imported context, including the retained export
and row, native references, reactions/status, and attachment references. Details
open in place without reloading the page or fetching more content, including
without JavaScript. Attachment references are not opened or fetched.

Use **Open evidence** beside a message's source for its full original, findings,
integrity details, native reply/thread links, and permitted actions.
**View in conversation** on that evidence page returns to the exact message with
its details open, including beyond the first page, and retains the export filter
and conversation list page when following this round trip.

In the evidence reader's **Info → Imported source context**, native thread
messages and reply candidates have independent 25-record pages in retained source
order. Both lists cover the complete same-case, same-export population; supplied
native IDs and multiple reply matches remain unverified source references.

### Retained call material

A call can be saved without content, with a supplied transcript, with one audio
file, or with both. Initial material is saved atomically with the call. Later,
owners/editors choose **Add transcript or audio** to append another retained
addition. Each addition is a `CallEvidenceAttachment`, with its own case, UUID,
uploader, creation time, unique submission ID, checksum, and exactly one phone-call
or FaceTime parent. An addition must contain transcript text, audio, or both.
There is no edit, replacement, archive, or individual deletion action for an
attachment. Add a further transcript to record a correction; earlier material
stays visible. Archiving the call retains all its material and still permits
owners/editors to add material. Viewers can inspect and download it.

Transcript text preserves the server-received text without trimming; browser
line-ending handling still applies. Transcripts are supplied material, not
verified facts or automatic transcription. One file is accepted per request;
`PANDECT_MAX_FILE_BYTES` and `PANDECT_MAX_TEXT_BYTES` apply to recordings and
transcripts respectively. Calls use ordinary forms, not the bulk-file queue.
After an uncertain submission, retry with the original submission ID and content;
new additions need new IDs. Failed file submissions can require reselection.

**Download communication record** returns the canonical JSON whose UTF-8 bytes
produce the record's SHA-256: sorted keys, compact separators, unescaped Unicode,
participant UUIDs, UTC ISO event time or null, and original message text or call
duration as integer microseconds or null. Schemas are `pandect.text-message.v1`,
`pandect.phone-call.v1`, and `pandect.facetime.v1`. Call checksums describe the
original call record and stay unchanged as content is added.

Each addition independently hashes a `pandect.call-attachment.v1` JSON record
containing the case/parent identity, transcript, and optional audio metadata and
byte checksum. **Info** exposes this record for download;
**Download transcript** returns its exact UTF-8 text, and **Download audio** returns
its original bytes as an attachment. All routes recheck current access and parent
membership and use private/no-store caching and `nosniff`; storage URLs stay private.
The collection searches participant names/contact details and audio filenames
without counting a call more than once when several attachments match.

The three models use existing scoped admin permissions. Calls show retained
attachments read-only, without storage URLs; **View on site** opens the product
flow for adding material. Attachments are not independently registered in admin.
Whole-case admin deletion also requires `phone.delete_callevidenceattachment`
when retained call attachments exist, alongside the existing per-model permissions.

## Location observations

Choose **Location** to capture an address or a pair of coordinates. Choose
**Person location** for a reported association with an existing person in the
same case. Both types inherit every shared evidence field, including **Source**,
source URL, source description, collection time, uploader, checksum, and archive
attribution. The person selector includes only people in the current case;
forged selections are rejected by forms and normal model validation/saves.

An address can contain up to 20,000 characters and is preserved without
trimming. Coordinates use WGS 84 decimal degrees with up to six decimal places.
Supply both latitude (-90 to 90) and longitude (-180 to 180), or neither. At
least a non-whitespace address or a coordinate pair is required. Zero is a valid
coordinate. Optional **Observed at** records the time reported by the
source, separately from source collection time and the time the record was
added. Unknown values remain unknown. The map and location analysis distinguish
reported coordinates from derived estimates.

The address, coordinates, observation time, and person link are immutable
originals. Correct source details through **Edit details**. Create
another record to correct the original observation. Person names shown in the
reader reflect the current person record, while its retained UUID identifies the
original association. A reported association is not proof of identity or
presence.

**Download original** returns the canonical UTF-8 JSON used for SHA-256: sorted
keys, compact separators, unescaped Unicode, coordinates as six-place decimal
strings (zero always positive), missing coordinates/times as JSON null, and
observation time as a UTC ISO timestamp. The schema is `pandect.location.v1` or
`pandect.person-location.v1`; the latter also includes `person_id`. Shared editable
metadata, including Source, is separate from this original and its checksum.
Authorized viewers can read/download archived observations as with other evidence.

New vehicle observations normalize a signed zero speed to positive zero before
hashing, matching PostgreSQL's retained numeric representation. This does not
rewrite existing originals or checksums.

Original downloads use `application/octet-stream` with an attachment filename so
browsers save them consistently, including Linux WebKit. Location, communication,
and call-attachment records keep their `.json` filenames and canonical bytes;
captured text and transcripts keep their `.txt` filenames and UTF-8 bytes.

Retained person-location evidence restricts individual person deletion,
including when archived. This protects the original association. Confirmed
whole-case deletion still cascades to both the evidence and its person. There is
no separate shared location table. Individual deletion is restricted to the
superuser workflows described here.

## Permanent deletion

Superusers with current case access can choose **More → Delete evidence**.
The product route requires no staff status; scoped admin deletion additionally
requires active staff. Ordinary staff/model permissions do not grant this ability,
and superusers cannot inspect or delete inaccessible cases.

The confirmation names the record, explains original/derived-file removal, and
requires an explicit checkbox and CSRF-protected POST. It works without JavaScript;
Cancel returns to the evidence reader and success to the collection, preserving filters.
The inventory lists every affected source, derivative, import/review record,
analysis, and stored file with identifiers and filenames/checksums. It shows 50
entries per page and offers a complete private JSON attachment. The signed
confirmation binds user, case, target, and inventory; changes require another
review and confirmation. GET never deletes.

Deletion removes original/call content, all runs selecting it (including attachment
sources), results/reviews/search text/generated files, and downstream dependent
analysis. Multi-source runs are removed in full; other source originals, people,
vehicles, and unrelated analysis remain. Source-document deletion also includes
imported vehicle observations/messages, import batches, staged rows, and
communication context, including archived derivatives. References from retained
runs to removed prior runs are cleared and listed as inventory updates.

`core.evidence.deletion` checks the full cascade before displaying private names
and again under the case lock; inconsistent cross-case links block deletion
without exposing foreign material. Product/admin use the same service and record
deletion in Django admin history. Database deletion is atomic, and unplanned ORM
deletes remain subject to model `RESTRICT` links.

The case lock serializes deletion with analysis/import publication. Removed queued
jobs become no-ops; running jobs cannot recreate sources or publish derivatives.
File cleanup waits for commit: rollback retains files, and storage failures use
[manual recovery](#private-storage-and-deletion). Application deletion does not
remove operator backups or external-provider retention.

## Django admin

Concrete evidence models are available under **Evidence** at `/admin/`, subject
to the [shared admin gates](cases.md#django-admin). Linked people,
vehicles, import batches, and source documents must belong to the
evidence's case. Admin and product readers exclude inconsistent records created
through trusted maintenance writes. Owner-only case deletion still counts
all evidence belonging to the case, without displaying inconsistent source details.

Creation assigns the signed-in operator as uploader. Product and admin forms
share file/text limits and preserve original text and addresses without trimming.
Existing originals, case, uploader, integrity fields, imported source values, and
saved descriptions are read-only. Declared technical metadata remains editable.
Change **Is archived** to archive or restore; the server records each actual
transition's operator and time. Admin history is not a versioned evidence journal.

Original filenames appear without storage paths or backend URLs. **View on site**
opens the authorized product reader/download workflow, which additionally requires
verified email. Individual **Delete** requires a superuser with current case
access and uses the [confirmed deletion workflow](#permanent-deletion).
Owner-confirmed whole-case deletion remains available through the case admin's
contained-model permission checks and post-commit cleanup.

## Creating and editing records

Use the documented environment to open `python manage.py shell`. This example
assumes `case` and `actor` have already been resolved and authorized by the caller:

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

Normal creation calculates integrity metadata, ignoring supplied checksum, size,
and original-filename values. Files must be nonempty, named Django file objects;
existing storage paths and another record's `FieldFile` cannot be reused. Hashing
reads in 64 KiB chunks and restores the input position. Text is not stripped,
normalized, or rewritten. Model callers may retain repeated copies as separate
records; product uploads require the duplicate review described above. Use
`full_clean()` for field validation before saving; as with
ordinary Django models, `save()` does not perform blanket model validation.
PostgreSQL constraints enforce required content, relationships, checksums, and
numeric bounds.

Normal validation and saves reject changes to the case, uploader, creation time,
original text/file/location/person link, original filename, byte size, declared
content type, and checksum, and submission ID. A changed original must become a
new record. Source names/descriptions, collection dates, and optional
type metadata may be corrected. Removing a collaborator does not remove their
retained evidence or attribution. The uploader uses `PROTECT`: users with
evidence cannot be deleted until that evidence is removed.

UUIDs are assigned before saving. A new instance cannot replace an existing
record with the same ID, including through forced updates; creation always
inserts a new row. Saving a deleted instance does not recreate it.

These safeguards cover model `clean()` and `save()`. Queryset/bulk writes, raw SQL,
`FieldFile.save()`/`delete()`, and direct storage writes bypass some or all model
safeguards and must be treated as trusted maintenance operations. In particular,
do not use bulk creation or queryset updates to ingest or replace originals.
This is application-level preservation, not storage-level write protection.

## Private storage and deletion

Files use Django's default storage backend. Locally, `PANDECT_MEDIA_ROOT` defaults
to `data/media`; Compose persists `/app/data` in `app_data`. A generated storage
key has the form `evidence/cases/<case_id>/<model_name>/<uuid>`, without a user
filename or extension. No code relies on a filesystem-only `.path` API. Hosted
operators can configure Django's `STORAGES` with a private backend; no hosted
storage dependency or credentials are installed by this change.

Product upload/detail/download/archive/delete routes check current case access and
parent-case membership. File responses use attachment disposition,
`application/octet-stream`, `nosniff`, and no-store caching; text attachments
reproduce the saved UTF-8 body. Admin never exposes storage URLs; no public media
route exists.
Do not expose the media directory or use a public storage bucket. WhiteNoise
serves application assets only and never private originals. Future exports and
jobs must enforce the same current access boundary. `uploaded_by` records
attribution and does not authorize an operation; the ORM remains a trusted
backend interface rather than an end-user permission boundary.

Trusted maintenance can delete evidence through the ORM, which removes its row;
ordinary users have no individual deletion path. Superusers use the confirmed
product or admin workflows above. Deleting a case cascades to all its active and
archived evidence. Each file-bearing app registers a `post_delete` receiver
using `core.storage.schedule_original_deletion`. It removes the original through
the storage API only after database commit,
including queryset/cascading deletion. A rolled-back deletion preserves both the
row and file. Text and location originals disappear with their database records.
Files belonging to other cases are preserved.

Storage failures do not undo a committed deletion or prevent cleanup of other
files. The `core.storage` logger reports a generated storage key without the
original filename, content, or backend exception text. There is no automatic retry
queue. After resolving storage availability, operators can retry the exact logged
key in the configured application's management shell:

```python
from django.core.files.storage import default_storage

default_storage.delete(
    "evidence/cases/CASE_UUID/documentevidence/GENERATED_KEY_FROM_LOG"
)
```

Database and storage writes are not atomic. When a new file-evidence or call-audio
model save fails, its database save is rolled back before the newly written file
is removed. Existing originals are never removed by failed metadata saves, and a
cleanup failure preserves the original save error while logging the generated key.
A later rollback of a caller's outer transaction remains that caller's
responsibility; the CSV importer handles its own batch rollback. Uncertain commit
outcomes retain the file, since the row may have committed. Those cases and a
process crash can still leave an orphaned file or interrupt deletion cleanup.
Retain restricted operational logs for manual recovery and reconcile
storage against database references before removing suspected orphans. Do not
delete files merely because an upload or transaction reported an error.

## Migration and backup

Domain apps own evidence tables; `core` owns shared processing tables. Use the
[fresh migration baseline](architecture.md#primary-keys-and-the-initial-schema).
Back up PostgreSQL and private storage together before upgrades: database-only
backups omit file originals. See [operations](operations.md).

References: [Django abstract models](https://docs.djangoproject.com/en/6.1/topics/db/models/#abstract-base-classes),
[file storage](https://docs.djangoproject.com/en/6.1/topics/files/), and
[commit callbacks](https://docs.djangoproject.com/en/6.1/topics/db/transactions/#performing-actions-after-commit).
