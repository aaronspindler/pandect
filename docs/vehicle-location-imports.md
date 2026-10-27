# Vehicle locations and Asset Location CSV imports

Owners and editors can add **Vehicle location** evidence inside a case, or choose
**Import vehicle locations** from Vehicles or Add evidence. Viewers can inspect
retained sources, reviews, imported observations, and private downloads. Every
vehicle choice and object request is scoped to the current case; superusers have
no case-access bypass.

## Import workflow

1. Choose a UTF-8 Asset Location CSV and its export timezone. The timezone must
   come from the export configuration, not the reported address. Supply an
   optional source name/citation and explicitly confirm that **Created Time**
   represents the reported observation time.
2. **Retain CSV and review** saves the exact file as Document evidence with its
   SHA-256 and creates a retained import record. The CSV may have a UTF-8 BOM.
   No location observations exist yet. Leaving the review retains the source;
   reopen its Document evidence and choose **Resume vehicle location review**.
3. Inspect each row's original values and interpreted UTC timestamps. Select an
   existing case vehicle for every distinct `(Asset, Serial Number)` pair.
   Assets do not automatically create vehicles or overwrite vehicle details.
4. Confirm the vehicle associations and import. Possible duplicates require an
   additional explicit acknowledgement. All observations and completion status
   commit together. A failure retains the source and adds no observations.

This flow works without JavaScript. Correct invalid rows in a new CSV and upload
it; the earlier source stays retained. Repeating a completed confirmation returns
the saved result. Repeating an upload with its original submission ID, identical
file, uploader, case, and timezone reopens its review without retaining another
file. Changing those values requires a new submission. An owner or editor can
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
| Speed | Optional nonnegative `km/h` value, up to six integer and three fractional digits. A real zero stays zero. Raw text is also retained. |
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
archived observations. They are not proof of the same event or vehicle. Review
and explicitly retain them as separate observations, or leave the review and
prepare a corrected CSV. No fuzzy matching or automatic cross-export merging
occurs. Exact retries of the same completed batch never add rows again.

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
record number, export timezone, and parser version `asset-location.v1`. Original
CSV bytes remain downloadable from the linked Document evidence. Shared editable
source metadata and current vehicle display details are outside this checksum.
Future parser versions must deliberately support or reject pending old imports;
they must not reinterpret a pending review silently.

Archiving retains the observation and all links. Retained observations prevent
individual vehicle deletion, and the import protects its source document. Only
owner-confirmed whole-case deletion removes these through the product, with file
cleanup after commit. The additive `evidence/0004_vehicle_locations` migration
creates the new tables and constraints; no existing records are rewritten or
runtime data reset. Apply it through the documented development/upgrade workflow.

## Operational bounds and scope

Imports are limited to **250 data rows and 1 MiB**, additionally respecting a
smaller `PANDECT_MAX_FILE_BYTES`. The upload handler streams with this smaller
limit on the importer route; parsing also bounds the bytes it reads. Addresses
are limited to 20,000 characters, triggers to 2,000, and other cells to 255.
Parsing and completion run synchronously within these bounds. Completion locks
the case and import, rechecks current edit permission, validates mappings and
source integrity, and commits all rows in one database transaction. Do not raise
these bounds to support lengthy imports; larger ingestion needs a background job
workflow with progress and recoverable retries first.

There is no external lookup, geocoding, AI, tracking verification, map, or timeline
in this feature. The sample format alone does not establish a vendor's timestamp,
event, duration, or identifier semantics. The importer records the investigator's
explicit time/vehicle interpretation alongside source claims.
