# Vehicles

`vehicles.Vehicle` is an investigator-maintained record belonging to exactly one
`cases.Case`. Use `case.vehicles.all()` for its vehicles. Vehicle records capture
reported details and investigative work; matching plates or VINs do not establish
identity, ownership, or a person's association with a vehicle. Records are not
automatically linked to people or other vehicles. Investigators can explicitly
link reported location evidence to a same-case vehicle.

## Investigator workflow

Open a case and choose **Vehicles**. Owners and editors can **Add vehicle**, enter
known details, and save. At least one make, model, license plate, or VIN is
required; partial details are allowed. Viewers can search and inspect records.
The list searches make, model, exact numeric year, color, plate, VIN, and notes,
with 25 records per page. Returning from a detail/edit screen preserves the list
query and page.

Choose **Edit vehicle** to correct details or notes. If another save changed the
record, the form preserves the draft and displays all latest saved details.
**Save reviewed changes** explicitly resubmits it. A deleted record is never
recreated by a stale form. Case reassignment is not supported.

Only the case owner can delete an individual vehicle after a separate named-target
confirmation. This retains evidence, people, other vehicles, login accounts, and
the case. Whole-case deletion also removes its vehicles, and its confirmation
shows the vehicle count. Ordinary investigators do not need staff permissions.
Retained vehicle-location observations, including archived evidence, prevent
individual vehicle deletion. Whole-case deletion removes both together.

The vehicle detail page lists up to 25 recent reported locations. Owners and
editors can add **Vehicle location** evidence or choose **Import vehicle
locations** to review an Asset Location CSV and explicitly map source assets to
case vehicles. Source identifiers never overwrite plates/VINs or establish
identity, ownership, or presence. See [Vehicle locations and CSV
imports](vehicle-location-imports.md) for formats, retention, limits, and recovery.

## Fields and validation

| Field | Behavior |
| --- | --- |
| `id` | Automatically generated, non-editable UUIDv4 primary key. |
| `case` | Required case foreign key; deleting the case deletes its vehicles. |
| `make`, `model` | Optional text, up to 150 characters each. |
| `year` | Optional integer from 1 through 9999; unknown years use `NULL`. |
| `color` | Optional text, up to 100 characters. |
| `license_plate`, `vin` | Optional text, up to 64 characters each. Partial or historical identifiers are accepted. |
| `notes` | Optional multiline investigator notes for sources, uncertainty, jurisdictions, and conflicting observations. |
| `created_at` | Automatically set on creation. |
| `updated_at` | Automatically updated on model saves; queryset updates must set it explicitly. |

A PostgreSQL constraint requires a non-whitespace make, model, plate, or VIN,
including on bulk writes. Year, color, and notes alone are insufficient. A second
constraint enforces the year range. Missing text uses empty strings, not `NULL`.
Identifiers are not unique, within or across cases. No country-specific plate
validation, VIN verification, normalization, ownership lookup, or merging occurs.

As with ordinary Django models, call `full_clean()` before ORM saves to validate
field lengths; `save()` does not automatically run model validation. Product and
admin forms validate automatically. Display names use the year (if known), make,
and model, falling back to plate and then VIN. Timestamps and Django admin history
are not a versioned evidence or vehicle-assertion history.

## Django admin and authorization

Open **Vehicles → Add vehicle** in `/admin/`. Creation allows only owned or
editor-access cases. Existing case and timestamp fields are read-only, even when
another case is owned by the same user. Bulk actions are disabled.

Active staff status and Django model permissions apply in addition to case access:

| Operation | Case requirement | Global permission |
| --- | --- | --- |
| List, search, inspect, history | Owner, viewer, or editor | `vehicles.view_vehicle` or `vehicles.change_vehicle` |
| Create | Owner or editor of the selected case | `vehicles.add_vehicle` |
| Edit | Owner or editor | `vehicles.change_vehicle` |
| Delete | Owner only | `vehicles.delete_vehicle` |

Lists, searches, direct URLs, and submitted case choices are scoped server-side.
Revocation and role downgrades affect subsequent requests in existing sessions.
Superusers have no case-access bypass. Admin deletion of a case with vehicles
also requires `vehicles.delete_vehicle` in addition to the other affected model
permissions.

Product views require an active, email-verified account and current case access;
all vehicle object paths also require the object to belong to the URL's case.
Mutations recheck permissions under a case lock and require CSRF-protected POST.
Future ORM callers, exports, jobs, and integrations must enforce current case
permissions themselves.

## Migration and scope

`vehicles/0001_initial` adds the vehicle table and constraints, depending on
`cases/0002_case_interfaces`. Apply the additive migration using the
[documented development workflow](development.md), or start the updated Compose
image with `make run`. Existing records and migrations are unchanged; no data
conversion, database reset, new dependency, or runtime upgrade is required.

Vehicle-location evidence and bounded CSV ingestion are implemented through the
additive `evidence/0004_vehicle_locations` migration. Person/vehicle relationships,
external lookups, public APIs, and analysis remain planned.
