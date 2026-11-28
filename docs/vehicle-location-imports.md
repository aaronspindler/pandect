# Vehicle locations and Asset Location CSV imports

Owners/editors add **Vehicle location** evidence or choose **Import vehicle
locations** from Vehicles/Add evidence. Viewers inspect sources, reviews,
observations, and private downloads. Every vehicle choice/request is case-scoped;
superusers have no access bypass.

## Import workflow

1. Choose the UTF-8 Asset Location CSV, an optional vehicle, and the export
   timezone. Use the timezone in the export account's profile. The form starts
   at **America/Toronto**. Starting from a vehicle preselects it. Optionally
   supply a citation and confirm that **Created Time** represents observation
   time.
2. **Upload CSV** retains the exact file as a Document and queues review
   preparation. Progress updates automatically with JavaScript. **Refresh
   status** works without JavaScript and **Retry updates** resumes paused
   automatic updates. Return from the Document with **Continue vehicle location
   import**.
3. Review original cells and interpreted times. The export timezone governs
   parsing, while the [account or device timezone](cases.md#account-settings)
   governs display. Confirm a vehicle for each Asset/Serial Number pair. The
   upload choice preselects each pair. **Add a vehicle** preserves your review
   choices. Apply the new vehicle to the relevant pairs when you return.
4. Review possible duplicates and confirm **Import locations**. Select **Keep
   the possible duplicates as separate observations** to retain matches. The
   default skips matches. Publication commits all retained observations and
   completion together. Failure retains the CSV without adding observations.

Use **Rows to correct** to inspect invalid rows at their original row numbers.
Upload a corrected CSV to proceed. Reconfirming a completed import returns
its saved result. Retrying identical file, uploader, case, timezone and vehicle
with the same submission ID reopens review without duplicating the file.

Any owner/editor can finish a collaborator's review. Observations attribute the
confirmer separately from the source uploader. Workers recheck edit access and
verified email during preparation/publication. A verified owner/editor can retry
if the original requester loses access/verification.

Completion shows imported and skipped counts, **View source CSV**, and **Open
case map** when observations were retained. **Imported observations** links to
each original and its source row, receipt time, asset labels and parser
provenance.

## Supported columns and interpretation

Use exactly these nine headers in any order. Pandect accepts a UTF-8 BOM and
standard CSV quoting, skips blank records, and shows malformed rows that block
publication.

| Column | Interpretation |
| --- | --- |
| Asset | Retained source label; either this or Serial Number must be nonblank. |
| Serial Number | Retained text, including leading zeros; an asset/device identifier, never a VIN. |
| Address | Required reported address, preserved without trimming; no inferred coordinates. |
| Speed | Optional nonnegative `km/h`, at most six integer/three fractional digits. Blank or `-` means unavailable; real zero stays zero. Raw text is retained. |
| Heading | Optional N, NE, E, SE, S, SW, W, or NW. Case/outer spaces are accepted; original retained. |
| Trigger | Retained description; no inferred engine state/activity. |
| Time Summary | Retained text; no inferred duration, start/end time, or presence interval. |
| Created Time | Required timestamp interpreted in the selected export timezone and stored as UTC `observed_at`. |
| Received Time | Optional timestamp interpreted in the selected export timezone and retained as `received_at`, separate from collection and upload times. |

New imports accept both `2026-09-16, 2:57:22 PM` (12-hour) and
`2026-09-16 14:57:22` (24-hour) in either timestamp column.

Every row requires an asset/device identifier and address. Ambiguous autumn and
nonexistent spring times require an unambiguous new export; no offset is chosen
silently. Invalid dates, units/headings, nulls, oversized fields, UTF-8, or CSV
produce recoverable errors.

Identical rows within a file or retained imports in the same case/export timezone
are **possible duplicates**, including archived observations; matches do not prove
the same event/vehicle. By default, retain the first CSV occurrence and skip later
copies; an existing observation causes all matching rows to be skipped. Check
**Keep the possible duplicates as separate observations** to retain all. Import
rechecks under the case lock, so intervening imports/deletions can change skips.
All-skipped imports complete with zero observations; the full CSV/review remains.
There is no fuzzy matching/cross-export merging. Completed-batch retries add no rows.

## Retention and integrity

`geo.VehicleLocationEvidence` extends abstract `geo.LocationContent` with a
required same-case vehicle. Manual records use ordinary location/source fields;
imports also retain batch, logical CSV record number (header is 1), every cell,
normalized speed, and receipt time. These originals are immutable; metadata edits
cannot replace them.

**Download original** returns canonical UTF-8 `pandect.vehicle-location.v1` JSON:
vehicle UUID, location, optional telemetry, row, source document UUID/checksum,
record number, export timezone, and the parser version used. The linked
Document retains exact CSV bytes. Editable metadata/current vehicle details are
outside the checksum. Future parsers must support or reject pending old imports
explicitly, never silently reinterpret them.

New imports use `asset-location.v3`, which accepts both timestamp formats.
Existing v1 and v2 imports keep their original 12-hour timestamp rule. V2 and v3
accept `-` in Speed as unavailable and retain the cell. If an older import rejects
a format now supported by v3, start a new import with the same CSV.

Archiving retains links; observations protect individual vehicle deletion.
Superusers with case access may confirm source-document deletion from Evidence
Details. Its inventory covers CSV, batch, staged/imported rows, dependent analysis,
and files; changed inventories require review again. People/vehicles remain.
Owner-confirmed whole-case deletion is also available. File cleanup follows commit.

`geo` owns `VehicleLocationImport`, `VehicleLocationImportRow`, and
`VehicleLocationEvidence`, their constraints, parser, jobs, and UI. Source files
remain `document.DocumentEvidence`; entities remain `vehicle.Vehicle`.
`geo.vehicle_imports` is the bounded parser. `geo.vehicle_import_jobs` owns
original retention, preparation, publication, and recovery.
`geo.tasks.process_vehicle_import` accepts IDs only. Templates live in
`geo/templates/geo/`; shared deletion uses
`core.evidence.deletion`.

## Operational bounds and scope

Limits are **100,000 data rows**, **100 MiB** (or the application upload limit
if lower) and **100 asset/serial pairs**. Review and results show 50 rows per
page. Addresses allow 20,000 characters, triggers 2,000, other cells 255 and
quoted logical records 64 Ki characters. Each phase has a ten-minute deadline,
60-second statement timeout and 10-second lock timeout. Split files at these
limits.

Preparation/publication use the [PostgreSQL worker](background-tasks.md) with
atomic domain/queue insertion. Publication locks case/import and rechecks edit
access, integrity, and mappings before committing inserts/completion together.
Repeated delivery/confirmation cannot duplicate rows or expose partial publication.

Keep the worker running; stopped workers leave jobs queued safely. Sanitized
failures allow three attempts per phase. Fix the cause before retrying; malformed
rows need a new CSV. Recover running jobs only after confirming the original
worker stopped; elapsed time is insufficient. Use the [fresh baseline](architecture.md#primary-keys-and-the-initial-schema)
and matching web/worker revisions: old task paths are incompatible. Commands never
reset runtime data automatically.

Publication queues applicable checks and refreshes [Trips](maps.md) for
newly retained observations. Trip preparation can queue configured
[private address lookup](maps.md#local-address-lookup) independently.

## Checking published observations

**Processing** shows the import's analysis batch and its individual checks.
Preparation works from published observations and excludes skipped duplicates.
Trips remain independent. See [processing](background-tasks.md#investigator-progress-and-completed-imports).
