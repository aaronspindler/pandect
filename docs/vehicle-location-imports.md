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
2. **Upload CSV** saves the exact file as Document evidence with its
   SHA-256 and creates a retained import record. The CSV may have a UTF-8 BOM.
   The worker prepares the review in the background, with progress and a
   **Refresh status** link. No location observations exist yet. Leaving retains
   the source; reopen its Document evidence and choose **Continue vehicle location
   import**.
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
An owner or editor can finish a review begun by another collaborator. Each
observation records the user who confirmed it separately from the original
file's uploader. The worker rechecks the requester's case edit access and
verified email during preparation and publication. A verified owner or editor
can retry if the original requester loses access or verification.

The import screen leads with progress and vehicle matching. **Source and time
interpretation** and the paginated source-row preview are native disclosures;
invalid rows open the preview for correction. On completion, **View findings**
opens the case findings and trips, with raw observations in a disclosure. Trips
use retained addresses/times without geocoding or review; see
[automatic vehicle trips](analysis-locations.md#automatic-vehicle-trips).
Individual observations retain **Original CSV row and interpretation**, including
receipt time, source cells, asset labels and parser provenance.

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
them all instead. Matches are checked again under the case lock before import.
Observations imported or deleted after review can change which rows are skipped.
If every row is skipped, the import completes with zero new
observations. The complete original CSV and reviewed rows stay retained.
No fuzzy matching or automatic cross-export merging occurs. Exact retries of
the same completed batch never add rows again.

## Retention and integrity

`geo.VehicleLocationEvidence` extends the abstract `geo.LocationContent` model
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
source and review stay unchanged. New imports default to parser v2 in the fresh
`geo` schema.

Archiving retains the observation and all links. Retained observations prevent
individual vehicle deletion. Superusers with current case access can confirm
source-document deletion from Evidence Details; its exact inventory includes the
CSV, import batch, every staged row and imported observation, and their dependent
analysis/files. A changed inventory requires another review. People and vehicles
are retained. Owner-confirmed whole-case deletion also remains available. Files
are removed after database commit. The `geo` app owns `VehicleLocationImport`,
`VehicleLocationImportRow`, and `VehicleLocationEvidence`, along with their
constraints, parser, jobs, and import UI. Source files remain
`document.DocumentEvidence` records; vehicle identities remain `vehicle.Vehicle`.

`geo.vehicle_imports` contains the bounded CSV parser;
`geo.vehicle_import_jobs` owns preparation, publication, and recovery. The
`geo.tasks.process_vehicle_import` queue entry point accepts identifiers only.
Import templates live under `geo/templates/geo/`; shared evidence deletion
continues through `core.evidence.deletion`.

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
assuming elapsed time means it is dead. The app reorganization requires the
[fresh migration baseline](architecture.md#primary-keys-and-the-initial-schema)
and an empty queue. Do not mix old and new web/worker revisions or reuse old
queued task paths. No application command automatically resets runtime data.

There is no external lookup, geocoding, AI, tracking verification, map, or timeline
in this feature. The sample format alone does not establish a vendor's timestamp,
event, duration, or identifier semantics. The importer records the investigator's
explicit time/vehicle interpretation alongside source claims.
