# Evidence models and original storage

The `evidence` app stores original investigation material. Each record belongs to
exactly one `cases.Case` through a required foreign key. A case can contain many
records of each type. There is no global evidence pool or concrete shared evidence
table: `Evidence` and `FileEvidence` are abstract Django models.

## Models and metadata

All four concrete models inherit these fields from `Evidence`:

- An automatically generated, non-editable UUIDv4 primary key (`id`).
- Required `case`, `uploaded_by`, and `title` (up to 255 characters).
- Automatic `created_at` and `edited_at` timestamps. `edited_at` changes on normal
  saves, including `save(update_fields=[...])`; creation time remains fixed.
- Optional `description`, `source_url`, `source_description`, and `collected_at`.
  Collection time describes source provenance, not the upload timestamp.
- Generated `sha256`, calculated from the original bytes or exact UTF-8 text.

| Model | Original | Optional metadata |
| --- | --- | --- |
| `ImageEvidence` | `file` | `width`, `height` in pixels |
| `AudioEvidence` | `file` | `duration` as a `timedelta`, `sample_rate` in Hz, `channel_count` |
| `DocumentEvidence` | `file` | `page_count` |
| `TextEvidence` | `body` | `language` tag, up to 35 characters |

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
original text/file, original filename, byte size, declared content type, and
checksum. A changed original must become a new record. Descriptions, source
descriptions, collection dates, and optional type metadata may be corrected.
Removing a collaborator does not remove their retained evidence or attribution.
The uploader uses `PROTECT`: users with evidence cannot be deleted until that
evidence is removed.

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

There is no evidence admin, upload screen, download route, or public media route.
Do not expose the media directory or use a public storage bucket. Future views,
admin, downloads, exports, and jobs must check the user's current case access on
every operation. Owners/editors may add material; viewers may inspect it. An
`uploaded_by` value records attribution and does not authorize an operation.
The ORM is a trusted backend interface, not an end-user permission boundary.

Deleting evidence removes its database row. Deleting a case cascades to all its
evidence. For file evidence, a `post_delete` receiver removes the original through
the storage API only after database commit, including queryset/cascading deletion.
A rolled-back deletion preserves both the row and file. Text originals disappear
with their database records. Files belonging to other cases are preserved.

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
database. See the [pre-release UUID baseline](architecture.md#primary-keys-and-the-initial-schema)
if the earlier integer initial migrations were already applied.
Back up before upgrades. Database backups include text and file metadata; file
originals require a matching private storage backup. See [operations](operations.md).

References: [Django abstract models](https://docs.djangoproject.com/en/6.1/topics/db/models/#abstract-base-classes),
[file storage](https://docs.djangoproject.com/en/6.1/topics/files/), and
[commit callbacks](https://docs.djangoproject.com/en/6.1/topics/db/transactions/#performing-actions-after-commit).
