# Evidence models and original storage

The `evidence` app stores original investigation material. Each record belongs
to exactly one `cases.Case` through a required foreign key. A case can contain
many records of each type. There is no global evidence pool or concrete shared
evidence table: `Evidence`, `FileEvidence`, and `LocationContent` are abstract
Django models.

## Investigator workflow

Inside a case, owners and editors choose **Add evidence** and select Document,
Image, Audio, Text, Location, or Person location. File categories describe the
submitted material; no parser verifies them. Enter a title, an optional
**Source** name/citation, and optional description, source URL, source
description, and collection time. Text captures one browser-submitted original;
to preserve a text file's exact bytes, upload the file as a Document instead.
Browser textareas may normalize line endings, but the server does not trim or
normalize received original text.

The normal form supports one record without JavaScript. With JavaScript, select
or drop a batch and review each entry's title, category, and source details before
uploading. Defaults are 20 files per batch, 100 MiB per file, and 1 MiB of UTF-8
captured text. Configured limits appear in the form; see
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

All six concrete models inherit these fields from `Evidence`:

- An automatically generated, non-editable UUIDv4 primary key (`id`).
- Required `case`, `uploaded_by`, and `title` (up to 255 characters).
- Automatic `created_at` and `edited_at` timestamps. `edited_at` changes on normal
  saves, including `save(update_fields=[...])`; creation time remains fixed.
- Optional `description`, `source` (a name/citation up to 255 characters),
  `source_url`, `source_description`, and `collected_at`.
  Collection time describes source provenance, not the upload timestamp.
- Generated `sha256`, calculated from the original bytes, captured UTF-8 text,
  or canonical location JSON.
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

File models inherit `file`, generated `original_filename` and `byte_size`, and
optional `declared_content_type` from `FileEvidence`. Uploaded-file content types
are captured when supplied; `ContentFile` callers may supply the declared type
explicitly. It is a claim, not a verified format. Numeric metadata defaults to
`NULL` (unknown); supplied counts and dimensions must be positive and duration
must be nonnegative. A zero duration is distinct from an unknown duration.

There is no image decoding, EXIF extraction, OCR, transcription, document parsing,
or automatic metadata verification. Image originals use `FileField`, without a
Pillow dependency. Text files may be stored as documents; `TextEvidence.body`
holds pasted original text. Descriptions and optional metadata are analyst input,
not extracted facts or AI results. File checksums do not prove a source is truthful.

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

Retained person-location evidence restricts individual person deletion,
including when archived. This protects the original association. Confirmed
whole-case deletion still cascades to both the evidence and its person. There is
no separate shared location table or individual evidence deletion route.

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
reproduce the saved UTF-8 body. No evidence admin or public media route exists.
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
Back up before upgrades. Database backups include text/location originals and
file metadata; file originals require a matching private storage backup. See
[operations](operations.md).

References: [Django abstract models](https://docs.djangoproject.com/en/6.1/topics/db/models/#abstract-base-classes),
[file storage](https://docs.djangoproject.com/en/6.1/topics/files/), and
[commit callbacks](https://docs.djangoproject.com/en/6.1/topics/db/transactions/#performing-actions-after-commit).
