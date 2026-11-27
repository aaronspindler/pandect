# Vehicle locations and Asset Location CSV imports

Owners/editors add **Vehicle location** evidence or choose **Import vehicle
locations** from Vehicles/Add evidence. Viewers inspect sources, reviews,
observations, and private downloads. Every vehicle choice/request is case-scoped;
superusers have no access bypass.

## Import workflow

1. Choose a UTF-8 Asset Location CSV, an optional vehicle for the whole file, and
   the export timezone (initially **America/Toronto**, editable).
   Starting from a vehicle's details preselects that vehicle, which you can change.
   **Back to vehicle** and **Cancel** return to the vehicle record.
   [miTrail's Profile Preferences](https://info.mitrail.com/en-US/profile-preferences-password-change-740866)
   describes account timezone selection, not a universal export timezone. Check
   that preference; never infer timezone from an address. Optionally supply a
   source/citation and explicitly confirm **Created Time** means observation time.
2. **Upload CSV** retains the exact file (optional UTF-8 BOM) as Document evidence
   with SHA-256 and an import record. The worker prepares review with progress
   that updates automatically with JavaScript. **Refresh status** is available
   without JavaScript, and **Retry updates** appears if automatic updates pause.
   No observations exist yet. To return later, open the
   Document and choose **Continue vehicle location import**.
3. Review paginated original cells and interpreted timestamps in your
   [account/device timezone](cases.md#account-settings); the export timezone still
   controls interpretation. The upload vehicle preselects every `(Asset, Serial Number)` pair. Change these choices or select all vehicles now if none was
   chosen. **Add a vehicle** preserves your vehicle choices and duplicate setting
   when you return from saving or cancelling. Choose the new vehicle for each
   applicable asset and confirm the reviewed associations. Assets never create
   vehicles or overwrite their details automatically.
4. Confirm associations/import. Leave **Keep the possible duplicates as separate
   observations** unchecked to skip matches, or check it to retain them. Background
   publication commits all observations and completion together, reporting imported
   and skipped counts with paginated results. Failure retains the source and adds
   no observations.

The flow works without JavaScript. Invalid rows require a corrected new CSV;
earlier sources remain retained. Reconfirming a completed import returns its
saved result. Retrying the original submission ID with identical file, uploader,
case, timezone, and upload vehicle reopens review without another file; changed
values require a new submission.

Any owner/editor can finish a collaborator's review. Observations attribute the
confirmer separately from the source uploader. Workers recheck edit access and
verified email during preparation/publication. A verified owner/editor can retry
if the original requester loses access/verification.

Screens lead with progress and matching, with the export timezone and Created Time
interpretation visible beside the review. **Rows to correct** lists the blocking
errors beside their original row numbers, with a separate link to all source rows.
**Source and time interpretation** and individual source rows use native
disclosures. The completed import leads with published and skipped counts, a
direct **View source CSV** link, and **Open case map** when observations are
retained. Expand **Imported observations** to see the individual records. Saved trips process
retained addresses/times without geocoding or review; see [automatic trips](analysis-locations.md#automatic-vehicle-trips).
Each observation retains **Original CSV row and interpretation**, including receipt
time, source cells, asset labels, and parser provenance.

## Supported columns and interpretation

Exactly these nine headers are required in any order. Duplicate/missing/additional
headers are rejected. Standard CSV quoting supports commas/newlines. Empty physical
records are ignored; malformed rows remain visible and block completion.

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

Limits are **100,000 data rows**, **100 MiB**,
and **100 asset/serial pairs**. Uploads stream within that byte limit; workers
bound reads, process chunks, and store private review rows in PostgreSQL. Review
and results show 50 rows per page. Addresses allow 20,000 characters, triggers
2,000, other cells 255, and logical records 64 Ki characters including multiline
cells. Each phase has a ten-minute deadline, sixty-second statement timeout, and
ten-second lock timeout. Split files that reach these limits.

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

Publication queues saved trips and applicable analysis for newly added
observations. Bounded background lookups use the configured private or
case-approved external geographic service. Observations feed [Trips](maps.md).
CSV format alone establishes no vendor timestamp, event, duration, or identifier
semantics; the investigator's explicit interpretation is retained.

## Checking published observations

**Processing** shows the import's analysis batch and its individual checks.
Preparation works from published observations and excludes skipped duplicates.
Trips remain independent. See [processing](background-tasks.md#investigator-progress-and-completed-imports).
