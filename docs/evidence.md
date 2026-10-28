# Evidence models and original storage

The `evidence` app stores original investigation material. Each record belongs
to exactly one `cases.Case` through a required foreign key. A case can contain
many records of each type. There is no global evidence pool or concrete shared
evidence table: `Evidence`, `FileEvidence`, `LocationContent`, `CommunicationEvidence`, and
`CallEvidence` are abstract Django models.

## Investigator workflow

Inside a case, owners and editors choose **Add evidence** and select Document,
Image, Audio, Text, Location, Person location, Vehicle location, Text message,
Phone call, or FaceTime. File categories describe the
submitted material; no parser verifies them. Enter a title, an optional
**Source** name/citation, and optional description, source URL, source
description, and collection time. Text captures one browser-submitted original;
to preserve a text file's exact bytes, upload the file as a Document instead.
Browser textareas may normalize line endings, but the server does not trim or
normalize received original text.

Owners and editors can choose **Bulk add files** from the case overview,
Evidence collection, or Add evidence form. Select or drop documents, images,
and audio together, then review titles, suggested categories, and source details.
**Add more files** appends to the queue without replacing entries or edits.
Filenames supply initial titles; browser-declared image/audio types suggest a
category, with Document as the fallback. These suggestions do not verify format.

Expand **Shared source details** to enter a source name, URL, description, and
collection time once. Changes follow pending files unless that individual field
has been edited, including cleared. **Use shared details** restores an entry's
shared source fields without changing its investigator description. Saved and
unconfirmed originals keep their submitted details unchanged. Shared controls
are disabled while uploading; file selection is also disabled while any save
confirmation remains unresolved.

The normal Add evidence form supports one record without JavaScript and retains
its existing enhanced file queue. The bulk page links to that single-record
fallback when browser support or JavaScript is unavailable. Defaults are 20 files
per batch, 100 MiB per file, and 1 MiB of UTF-8 captured text. Saved entries count
toward the bulk page's limit; **Start another batch** opens a fresh page after all
entries are added or removed. Configured limits appear in the form; see
[configuration](development.md#configuration) for operator settings.

Batch uploads send one file at a time and show uploading, saving, added, failure,
and uncertain-confirmation states. Successful entries remain saved. A known
validation rejection allows corrections; an interrupted confirmation must first
retry the same frozen submission so an already-saved original is not duplicated.
**Stop after this file** pauses pending entries. Queues live only in page memory;
reloading may require file reselection, and uploads are not resumable.

The Evidence collection searches title, descriptions, source name/details/URL,
original filenames, location addresses, and linked person names/contact details,
with type and Active/Archived/All filters. It uses 25 records per page. Wide
screens keep the record list beside the selected original/details; smaller
screens use the same canonical detail pages and browser navigation. Text
originals are shown as escaped read-only text. Files are downloaded through
currently authorized attachment routes; this release does not embed documents,
render image previews, or play audio.

Owners/editors can **Edit details** without replacing the original. Conflicting
metadata edits display newer saved values alongside the retained draft for an
explicit **Save reviewed changes**. **Archive** and **Restore** organize retained
material; current case members can still inspect/download archived evidence.
There is no individual evidence delete or purge route. Only owner-confirmed
whole-case deletion removes retained evidence through the product interface.

## Models and metadata

All ten concrete evidence models inherit these fields from `Evidence`:

- An automatically generated, non-editable UUIDv4 primary key (`id`).
- Required `case`, `uploaded_by`, and `title` (up to 255 characters).
- Automatic `created_at` and `edited_at` timestamps. `edited_at` changes on normal
  saves, including `save(update_fields=[...])`; creation time remains fixed.
- Optional `description`, `source` (a name/citation up to 255 characters),
  `source_url`, `source_description`, and `collected_at`.
  Collection time describes source provenance, not the upload timestamp.
- Generated `sha256`, calculated from the original bytes, captured UTF-8 text,
  or canonical location/communication JSON.
- `is_archived`, plus `archive_changed_at` and protected `archive_changed_by`
  attribution for the latest real archive/restore transition; this is not a full
  history journal.
- Optional, immutable, unique `submission_id` UUID for idempotent product uploads.
  Existing records and trusted maintenance callers may leave it null.

| Model | Original | Optional metadata |
| --- | --- | --- |
| `ImageEvidence` | `file` | `width`, `height` in pixels |
| `AudioEvidence` | `file` | `duration` as a `timedelta`, `sample_rate` in Hz, `channel_count` |
| `DocumentEvidence` | `file` | `page_count` |
| `TextEvidence` | `body` | `language` tag, up to 35 characters |
| `LocationEvidence` | `address` and/or `latitude` + `longitude`, optional `observed_at` | Shared source/description fields |
| `PersonLocationEvidence` | Same location fields plus a required `person` in the same case | Shared source/description fields |
| `VehicleLocationEvidence` | Same location fields plus a required same-case `vehicle`; optional immutable CSV row, speed, receipt time, and import provenance | Shared source/description fields |
| `TextMessageEvidence` | Required same-case `sender` and `recipient`, one `body`, optional `occurred_at` | Shared source/description fields |
| `PhoneCallEvidence` / `FaceTimeEvidence` | Required same-case `sender` and `recipient`, optional `occurred_at` and `duration` | Shared source/description fields; retained attachments added separately |

File models inherit `file`, generated `original_filename` and `byte_size`, and
optional `declared_content_type` from `FileEvidence`. Uploaded-file content types
are captured when supplied; `ContentFile` callers may supply the declared type
explicitly. It is a claim, not a verified format. Numeric metadata defaults to
`NULL` (unknown); supplied counts and dimensions must be positive and duration
must be nonnegative. A zero duration is distinct from an unknown duration.

Optional [case analysis](analysis.md) extracts bounded document/image/audio content
and retains derived metadata, OCR, and transcripts separately. Image originals
continue to use `FileField`; parsing never replaces supplied metadata. Text files may be stored as documents; `TextEvidence.body`
holds pasted original text. Descriptions and optional metadata are analyst input,
not extracted facts or AI results. File checksums do not prove a source is truthful.

## Communications and retained call material

Choose **Text message**, **Phone call**, or **FaceTime** inside the case's Add
evidence workflow. Sender and recipient are required existing people in that
case; they may be the same person. Add people first when the case has none.
A message captures exactly one original message, not a conversation thread.
Optional **Sent at (UTC)** or **Call started at (UTC)** records reported event
time, separately from source collection and upload time. Calls also accept an
optional reported duration such as `00:01:30`; zero is valid and blank is unknown.
No time, duration, or identity is inferred.

Participants, reported time/duration, and message text are immutable originals.
Names shown in the reader follow current person records, while retained UUIDs
identify the original associations. These associations do not verify identity.
Both participants are restricted to the same case by product/admin forms and
normal model validation/saves. Either participant link prevents individual person
deletion, including for archived evidence; whole-case deletion removes both.

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
byte checksum. Attachment integrity details expose this record for download;
**Download transcript** returns its exact UTF-8 text, and **Download audio** returns
its original bytes as an attachment. All routes recheck current access and parent
membership and use private/no-store caching and `nosniff`; storage URLs stay private.
The collection searches participant names/contact details and audio filenames
without counting a call more than once when several attachments match.

The three models use existing scoped admin permissions. Calls show retained
attachments read-only, without storage URLs; **View on site** opens the product
flow for adding material. Attachments are not independently registered in admin.
Whole-case admin deletion also requires `evidence.delete_callevidenceattachment`
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
coordinate. Optional **Observed at (UTC)** records the time reported by the
source, separately from source collection time and the time the record was
added. Unknown values remain unknown. There is no geocoding, map, tracking, or
presence verification.

The address, coordinates, observation time, and person link are immutable
originals. Correct a description or source through **Edit details**; create
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

Original downloads use `application/octet-stream` with an attachment filename so
browsers save them consistently, including Linux WebKit. Location, communication,
and call-attachment records keep their `.json` filenames and canonical bytes;
captured text and transcripts keep their `.txt` filenames and UTF-8 bytes.

Retained person-location evidence restricts individual person deletion,
including when archived. This protects the original association. Confirmed
whole-case deletion still cascades to both the evidence and its person. There is
no separate shared location table or individual evidence deletion route.

## Django admin

Vehicle location originals and the bounded Asset Location CSV import flow are
documented in [Vehicle locations and CSV imports](vehicle-location-imports.md).
Source CSVs remain retained Document evidence. Imported telemetry and associations
are immutable; ordinary description/source edits do not replace them.

All ten concrete evidence models are available under **Evidence** at `/admin/`.
Active staff status and the corresponding Django model permission are required
in addition to current case access. Lists, searches, history, and direct record
URLs include only cases the operator owns or shares, including for superusers.

| Operation | Case requirement | Model permission |
| --- | --- | --- |
| List, search, inspect originals and history | Owner, viewer, or editor | `view_<model>` or `change_<model>` |
| Add evidence | Owner or editor of the selected case | `add_<model>` |
| Edit metadata, archive, restore | Owner or editor | `change_<model>` |

Permission names use the `evidence` app and lowercase model names, for example
`evidence.add_documentevidence`. Case permissions alone do not grant evidence
permissions. Staff without an editable case cannot open an evidence creation form.
Removing or downgrading membership affects subsequent requests in existing sessions.

Choose a type, select an editable case, and supply its original and source details.
The uploader is assigned to the signed-in operator. Person-location choices
include people from editable cases; the selected person must belong to the
selected case, with mismatches rejected on the server. File/text limits also apply
in admin, and original text and addresses are preserved without trimming.

Existing originals, case, uploader, checksum, submission ID, and generated
timestamps are read-only. Declared technical metadata remains editable. Change
**Is archived** to archive or restore; the server records the operator and time of
each actual transition. Original filenames are shown without exposing storage
paths or backend URLs. **View on site** opens the normal evidence detail and
private download workflow, which requires verified email as well as current case
access. Admin adds and edits appear in Django admin history; this is not a
versioned evidence journal.

Individual deletion and bulk actions are disabled. Owner-confirmed whole-case
deletion in admin requires `cases.delete_case`, the `delete_<model>` permission
for every contained evidence type, and any other permissions listed on Django's
confirmation screen. It removes retained evidence, people, and original files
through the existing cascade and committed cleanup. No schema migration is needed
for these admin registrations.

The implementation uses Django's standard
[ModelAdmin hooks](https://docs.djangoproject.com/en/6.1/ref/contrib/admin/),
including the case deletion confirmation hook to preserve the whole-case cascade
while denying individual evidence deletion.

## Creating and editing records

Use the documented environment to open `python manage.py shell`. This example
assumes `case` and `actor` have already been resolved and authorized by the caller:

```python
from django.core.files.base import ContentFile
from evidence.models import DocumentEvidence, TextEvidence

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
text.description = "Analyst description, separate from the source text."
text.save(update_fields=["description"])
```

Normal creation calculates integrity metadata, ignoring supplied checksum, size,
and original-filename values. Files must be nonempty, named Django file objects;
existing storage paths and another record's `FieldFile` cannot be reused. Hashing
reads in 64 KiB chunks and restores the input position. Text is not stripped,
normalized, or rewritten. Repeated copies remain separate records without
deduplication. Use `full_clean()` for field validation before saving; as with
ordinary Django models, `save()` does not perform blanket model validation.
PostgreSQL constraints enforce required content, relationships, checksums, and
numeric bounds.

Normal validation and saves reject changes to the case, uploader, creation time,
original text/file/location/person link, original filename, byte size, declared
content type, and checksum, and submission ID. A changed original must become a
new record. Descriptions, source names/descriptions, collection dates, and optional
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

Product upload/detail/download/archive routes check current case access and
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
ordinary users have no individual deletion path. Deleting a case cascades to all
its active and archived evidence. For file evidence, a `post_delete` receiver
removes the original through the storage API only after database commit,
including queryset/cascading deletion. A rolled-back deletion preserves both the
row and file. Text and location originals disappear with their database records.
Files belonging to other cases are preserved.

Storage failures do not undo a committed deletion or prevent cleanup of other
files. The `evidence.signals` logger reports a generated storage key without the
original filename, content, or backend exception text. There is no automatic retry
queue. After resolving storage availability, operators can retry the exact logged
key in the configured application's management shell:

```python
from django.core.files.storage import default_storage

default_storage.delete(
    "evidence/cases/CASE_UUID/documentevidence/GENERATED_KEY_FROM_LOG"
)
```

Database and storage writes are not atomic. Failed or rolled-back creation can
leave an orphaned file; a process crash after deletion commits can also interrupt
cleanup. Retain restricted operational logs for manual recovery and reconcile
storage against database references before removing suspected orphans. Do not
delete files merely because an upload or transaction reported an error.

## Migration and backup

The initial evidence migration depends on the configured user model and
`cases/0001_initial`; it creates four UUID tables without requiring new
dependencies. Apply it with the usual documented `migrate` command on a fresh
database. See the [pre-release UUID
baseline](architecture.md#primary-keys-and-the-initial-schema) if the earlier
integer initial migrations were already applied. The additive evidence `0002`
migration adds archive attribution/state and submission IDs. Existing material
starts active with null archive attribution and submission ID; original bytes
and integrity metadata are unchanged. Apply pending migrations using the
documented upgrade workflow, without resetting existing databases. The additive
`0003_location_evidence_and_source` migration creates the two location tables
and adds optional `source` fields to all four existing evidence tables. Existing
records receive an empty source name; their source URLs, descriptions,
checksums, and originals remain unchanged. No reset or data import is required.
The additive `0004_vehicle_locations` migration creates vehicle-location evidence
and import provenance tables. It does not change existing records. The additive
`0005_communication_evidence` migration adds three communication tables and the
call-attachment table with native UUID keys; existing originals are unchanged.
Back up before upgrades. Database backups include text/location/communication
originals, transcripts, and file metadata; file originals require a matching
private storage backup. See
[operations](operations.md).

References: [Django abstract models](https://docs.djangoproject.com/en/6.1/topics/db/models/#abstract-base-classes),
[file storage](https://docs.djangoproject.com/en/6.1/topics/files/), and
[commit callbacks](https://docs.djangoproject.com/en/6.1/topics/db/transactions/#performing-actions-after-commit).
