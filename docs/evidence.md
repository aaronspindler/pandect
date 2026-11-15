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

The reader shows the original followed by **Findings**. Its header keeps
**Download original**, **Info**, and permitted **More** actions together. **Info**
opens source metadata, saved read-only descriptions, attribution, integrity,
attachment manifests, CSV interpretation, and paginated processing history.
**More** holds Edit details, Archive/Restore, additional analysis, and authorized
deletion. Empty optional values are omitted; imported observations keep the
source-row/import link beside the original. These disclosures work without JavaScript.

Single-record forms keep title/content visible. Optional provenance and technical
fields are in **Source details**, opened for edits and validation errors. Collection
**Filters** opens for nondefault type/state choices. These controls work without
JavaScript. Applicable local checks run on investigator additions; findings and
progress appear below the original. **Run checks** appears beside empty findings;
checks for populated findings are in **More**. Processing history is available in
**Info** and case Findings.
See [analysis](analysis.md#retained-processing) for supported checks and exceptions.

## Investigator workflow

Inside a case, owners and editors choose **Add evidence** to select or drop
documents, images, and audio together. **Add text** opens a text original;
**Import vehicle locations** opens CSV intake. **More ways to add evidence**
contains Location, Person location, Vehicle location, Text message, Phone call,
and FaceTime. Single-record forms keep all ten types in **Change type**, using
an explicit **Choose type** button that also works without JavaScript.

File categories describe the submitted material; no parser verifies them. Text
captures one browser-submitted original; to preserve a text file's exact bytes,
upload the file as a Document instead. Browser textareas may normalize line
endings, but the server does not trim or normalize received original text.

After selecting files, review their titles, suggested categories, and optional
source details before uploading. Queue and upload actions appear after selection;
**Stop after this file** appears while uploading.
**Add more files** appends to the queue without replacing entries or edits.
Filenames supply initial titles; browser-declared image/audio types suggest a
category, with Document as the fallback. These suggestions do not verify format.

The file picker comes first. Expand **Shared source details** to enter a source name, URL, description, and
collection time once. Changes follow pending files unless that individual field
has been edited, including cleared. **Use shared details** restores an entry's
shared source fields. Saved and
unconfirmed originals keep their submitted details unchanged. Shared controls
are disabled while uploading; file selection is also disabled while any save
confirmation remains unresolved.

When browser support or JavaScript is unavailable, Add evidence links to an
explicit single-document form. Each single-record form works without JavaScript;
its **Change type** selector provides the other evidence types. Defaults are 20 files
per batch, 100 MiB per file, and 1 MiB of UTF-8 captured text. Saved entries count
toward the bulk page's limit; **Start another batch** opens a fresh page after all
remaining files are added. Removing every pending file returns to the empty
picker. Configured limits appear in the form; see
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
screens keep the record list beside the selected evidence reader; smaller
screens use the same canonical detail pages and browser navigation. Text
originals are shown as escaped read-only text. Image evidence shows a resized
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
currently authorized attachment routes; this release does not embed documents
or play audio.

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

All ten concrete evidence models inherit these fields from `Evidence`:

- An automatically generated, non-editable UUIDv4 primary key (`id`).
- Required `case`, `uploaded_by`, and `title` (up to 255 characters).
- Automatic `created_at` and `edited_at` timestamps. `edited_at` changes on normal
  saves, including `save(update_fields=[...])`; creation time remains fixed.
- Optional `source` (a name/citation up to 255 characters), `source_url`,
  `source_description`, and `collected_at`. Previously saved `description`
  values remain readable and searchable.
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
| `image.ImageEvidence` | `file` | `width`, `height` in pixels |
| `audio.AudioEvidence` | `file` | `duration` as a `timedelta`, `sample_rate` in Hz, `channel_count` |
| `document.DocumentEvidence` | `file` | `page_count` |
| `text.TextEvidence` | `body` | `language` tag, up to 35 characters |
| `geo.LocationEvidence` | `address` and/or `latitude` + `longitude`, optional `observed_at` | Shared source fields |
| `geo.PersonLocationEvidence` | Same location fields plus a required `person` in the same case | Shared source fields |
| `geo.VehicleLocationEvidence` | Same location fields plus a required same-case `vehicle`; optional immutable CSV row, speed, receipt time, and import provenance | Shared source fields |
| `phone.TextMessageEvidence` | Required same-case `sender` and `recipient`, one `body`, optional `occurred_at` | Shared source fields |
| `phone.PhoneCallEvidence` / `phone.FaceTimeEvidence` | Required same-case `sender` and `recipient`, optional `occurred_at` and `duration` | Shared source fields. Retained attachments can be added separately. |

File models inherit `file`, generated `original_filename` and `byte_size`, and
optional `declared_content_type` from `FileEvidence`. Uploaded-file content types
are captured when supplied; `ContentFile` callers may supply the declared type
explicitly. It is a claim, not a verified format. Numeric metadata defaults to
`NULL` (unknown); supplied counts and dimensions must be positive and duration
must be nonnegative. A zero duration is distinct from an unknown duration.

[Case analysis](analysis.md) retains derived metadata, OCR, and transcripts
separately from originals and supplied metadata. `TextEvidence.body` holds captured
text; text files may be retained as Documents. File checksums establish byte
integrity, not authenticity.

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

Vehicle location originals and the bounded Asset Location CSV import flow are
documented in [Vehicle locations and CSV imports](vehicle-location-imports.md).
Source CSVs remain retained Document evidence. Imported telemetry and associations
are immutable. Source edits leave them intact.

All ten concrete evidence models are available under **Evidence** at `/admin/`.
The [shared admin gates](cases.md#django-admin) apply. Lists, searches, history,
and direct URLs include only cases the operator owns or shares.
Admin and product readers share the same relationship checks. Linked people,
vehicles, import batches, and source documents must belong to the
evidence's case. Inconsistent records created through trusted maintenance writes
are excluded from these views. Owner-only case deletion confirmation still counts
all evidence belonging to the case, without displaying inconsistent source details.

| Operation | Case requirement | Model permission |
| --- | --- | --- |
| List, search, inspect originals and history | Owner, viewer, or editor | `view_<model>` or `change_<model>` |
| Add evidence | Owner or editor of the selected case | `add_<model>` |
| Edit metadata, archive, restore | Owner or editor | `change_<model>` |
| Permanently delete individual evidence | Current case access and active staff superuser | Superuser only |

Permission names use the owning domain app and lowercase model name, for example
`document.add_documentevidence`, `image.view_imageevidence`,
`text.change_textevidence`, `audio.view_audioevidence`,
`geo.add_vehiclelocationevidence`, and `phone.view_phonecallevidence`. Case permissions alone do not grant evidence
permissions. Staff without an editable case cannot open an evidence creation form.
Removing or downgrading membership affects subsequent requests in existing sessions.

Choose a type, select an editable case, and supply its original and source details.
The uploader is assigned to the signed-in operator. Person-location choices
include people from editable cases; the selected person must belong to the
selected case, with mismatches rejected on the server. Product and admin forms
share file/text limit validation, and original text and addresses are preserved
without trimming.

The title, source name, and original are visible by default. Saved investigator
descriptions are read-only on existing records. Expand
**Additional source details** for the source URL, source description, and
collection date. Validation errors keep affected fields visible. Declared
metadata, imported source values, and integrity attribution use separate native
disclosures; retention controls remain visible.

Existing originals, case, uploader, checksum, submission ID, and generated
timestamps are read-only. Declared technical metadata remains editable. Change
**Is archived** to archive or restore; the server records the operator and time of
each actual transition. Original filenames are shown without exposing storage
paths or backend URLs. **View on site** opens the normal evidence detail and
private download workflow, which requires verified email as well as current case
access. Admin adds and edits appear in Django admin history; this is not a
versioned evidence journal.

Admin **Delete** uses the [shared permanent-deletion workflow](#permanent-deletion).
Bulk actions remain disabled.

Owner-confirmed whole-case deletion in admin requires `cases.delete_case`, the
`delete_<model>` permission
for every contained evidence type, and any other permissions listed on Django's
confirmation screen. It removes retained evidence, people, and original files
through the existing cascade and committed cleanup. The confirmation uses the
concrete models registered by each domain app.

The implementation uses Django's standard
[ModelAdmin hooks](https://docs.djangoproject.com/en/6.1/ref/contrib/admin/),
including the case deletion confirmation hook to preserve the whole-case cascade
for owners who are not superusers.

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
