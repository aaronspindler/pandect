# Vehicle locations and Asset Location CSV imports

Owners and editors can add **Vehicle location** evidence inside a case, or choose
**Import vehicle locations** from Vehicles or Add evidence. Viewers can inspect
retained sources, reviews, imported observations, and private downloads. Every
vehicle choice and object request is scoped to the current case; superusers have
no case-access bypass.

## Import workflow

1. Choose a UTF-8 Asset Location CSV, an optional case vehicle for the whole file,
   and its export timezone. The initial timezone is **America/Toronto** and remains
   editable to match the exporting account.
   [miTrail's Profile Preferences documentation](https://info.mitrail.com/en-US/profile-preferences-password-change-740866)
   tells users to set their timezone and does not establish a universal export
   timezone. Check the exporting account's preference, never infer it from an
   address. Supply an optional source name/citation and explicitly confirm that
   **Created Time** represents the reported observation time.
2. **Retain CSV and review** saves the exact file as Document evidence with its
   SHA-256 and creates a retained import record. The CSV may have a UTF-8 BOM.
   The worker prepares the review in the background, with progress and a
   **Refresh status** link. No location observations exist yet. Leaving retains
   the source; reopen its Document evidence and choose **Resume vehicle location
   review**.
3. Inspect the paginated original values and interpreted UTC timestamps. The
   upload vehicle preselects every distinct `(Asset, Serial Number)` pair; change
   individual choices if needed, or select them all here if none was chosen.
   Assets do not automatically create vehicles or overwrite vehicle details.
4. Confirm the vehicle associations and import. Leave **Keep the possible
   duplicates as separate observations** unchecked to skip matching rows, or
   select it to keep them. The worker imports the selected rows in the
   background. The result reports imported and skipped counts. All observations
   and completion status commit together. A
   failure retains the source and adds no observations. Completed results are
   paginated as well.

This flow works without JavaScript. Correct invalid rows in a new CSV and upload
it; the earlier source stays retained. Repeating a completed confirmation returns
the saved result. Repeating an upload with its original submission ID, identical
file, uploader, case, timezone, and selected upload vehicle reopens its review
without retaining another file. Changing those values requires a new submission.
An owner or editor can
finish a review begun by another collaborator; each observation records the user
who confirmed it, separately from the original file's uploader.

## Supported columns and interpretation

Exactly these nine headers are required, in any order. Duplicate, missing, or
additional headers are rejected. Quoted commas and newlines use standard CSV
rules. Empty physical records are ignored; malformed rows remain visible with
errors and block completion.

| Column | Interpretation |
| --- | --- |
| Asset | Retained source label; either this or Serial Number must be nonblank. |
| Serial Number | Retained text, including leading zeros. A source asset/device identifier, never a VIN. |
| Address | Required reported address, preserved without trimming. No coordinates are inferred. |
| Speed | Optional nonnegative `km/h` value, up to six integer and three fractional digits. Blank or `-` means unavailable, not zero. A real zero stays zero. Raw text is also retained. |
| Heading | Optional N, NE, E, SE, S, SW, W, or NW. Case/outer spaces are accepted, with the original retained. |
| Trigger | Retained source description, without inferring engine state or activity. |
| Time Summary | Retained text. No duration, start time, end time, or presence interval is inferred. |
| Created Time | Required `2026-09-16, 2:57:22 PM` format, interpreted in the explicitly selected timezone and stored as `observed_at` in UTC. |
| Received Time | Optional timestamp in the same format/timezone, retained separately as `received_at`. It is not source collection time or Pandect upload time. |

At least one asset/device identifier and an address are required for every row.
Ambiguous autumn times and nonexistent spring times are rejected rather than
silently choosing an offset. Obtain an unambiguous source export before importing
those rows. Invalid dates, unsupported units/headings, null characters, oversized
fields, invalid UTF-8, and malformed CSV produce recoverable errors.

Identical original rows within a file or in existing vehicle imports with the
same case and export timezone are flagged as **possible duplicates**, including
archived observations. They are not proof of the same event or vehicle. By
default, import the first occurrence in the CSV and skip later identical rows;
if a matching retained observation already exists, skip every matching row.
Select **Keep the possible duplicates as separate observations** to import
them all instead. Matches are checked again under the case lock before import,
so the skip choice also applies to observations imported after the review was
prepared. If every row is skipped, the import completes with zero new
observations. The complete original CSV and reviewed rows stay retained.
No fuzzy matching or automatic cross-export merging occurs. Exact retries of
the same completed batch never add rows again.

## Retention and integrity

`VehicleLocationEvidence` extends the existing abstract `LocationContent` model
with a required same-case `vehicle` link. Manual additions use the ordinary
location fields and shared source metadata. Imported records additionally retain
the import batch, logical CSV record number (header is 1), every original cell,
normalized speed, and source receipt time. These values are immutable originals;
editing descriptions/source details cannot replace them.

Private **Download original** returns canonical UTF-8 JSON with schema
`pandect.vehicle-location.v1`. It includes the vehicle UUID, location, optional
telemetry, original row, and import provenance: source document UUID and checksum,
record number, export timezone, and parser version `asset-location.v2`. Original
CSV bytes remain downloadable from the linked Document evidence. Shared editable
source metadata and current vehicle display details are outside this checksum.
Future parser versions must deliberately support or reject pending old imports;
they must not reinterpret a pending review silently.

Parser `asset-location.v2` accepts `-` in Speed as unavailable while retaining
the original cell. Existing `asset-location.v1` reviews and jobs keep their
original rules, including rejection of that placeholder; valid v1 imports can
still complete. To import a CSV rejected only for `-` speeds, start a new upload
of the unchanged original with the updated application. The earlier retained
source and review stay unchanged. Migration `evidence/0007_vehicle_import_parser_v2`
changes the default for new imports only; existing versions and checksums are
preserved. Apply it before restarting the updated web and worker processes.

Archiving retains the observation and all links. Retained observations prevent
individual vehicle deletion, and the import protects its source document. Only
owner-confirmed whole-case deletion removes these through the product, with file
cleanup after commit. The additive `evidence/0004_vehicle_locations` migration
creates the new tables and constraints; no existing records are rewritten or
runtime data reset. Apply it through the documented development/upgrade workflow.

## Operational bounds and scope

Imports are limited to **100,000 data rows and 100 MiB**, additionally respecting
a smaller `PANDECT_MAX_FILE_BYTES`. At most **100 distinct asset/serial pairs**
may be mapped in one import. The upload handler streams with the smaller byte
limit; the worker bounds its reads, processes chunks, and stores private review
rows in PostgreSQL. Review and result pages show 50 rows at a time. Addresses are
limited to 20,000 characters, triggers to 2,000, and other cells to 255. Logical
CSV records are bounded to 64 Ki characters, including quoted multiline cells.
Each processing phase has a ten-minute deadline, with sixty-second database
statement and ten-second lock limits; split files if these bounds are reached.

Preparation and publication use the existing Django Tasks/PostgreSQL worker,
with queue insertion in the same transaction as the originating domain change.
The worker rechecks current edit permission and source integrity. Publication
locks the case and import, validates mappings, and commits every observation and
completion in one transaction, using bounded bulk inserts. Original JSON and
checksums retain the existing imported-observation schema. No observations from a partial
publication become visible. Repeated task delivery and confirmation do not create
another set of observations.

Keep the worker running as described in [background tasks](background-tasks.md).
Queued work waits safely while it is stopped. Failures display a sanitized reason
and permit up to three attempts per phase. Retry after fixing the reported
problem; malformed source rows require a corrected new CSV. Recovery of work
left running requires confirmation that the original worker stopped, rather than
assuming elapsed time means it is dead. Retained CSVs and completed batches are
preserved by migration `evidence/0006_large_vehicle_location_imports`; it also
backfills a derived duplicate-search fingerprint without changing original JSON
or its checksum. Pending older imports show **Prepare review** on their existing
review page. Apply the migration before
starting updated web and worker processes. Do not mix old and new import workers
during upgrade.

There is no external lookup, geocoding, AI, tracking verification, map, or timeline
in this feature. The sample format alone does not establish a vendor's timestamp,
event, duration, or identifier semantics. The importer records the investigator's
explicit time/vehicle interpretation alongside source claims.
