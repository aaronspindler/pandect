# Vehicles

`vehicle.Vehicle` is an investigator-maintained record belonging to exactly one
`cases.Case`. Vehicle records capture reported details and investigative work;
matching plates or VINs do not establish
identity, ownership, or a person's association with a vehicle. Records are not
automatically linked to people or other vehicles. Investigators can explicitly
link reported location evidence to a same-case vehicle. The `vehicle` app owns
entity models, forms, views, templates, administration, and tests. The related
location evidence, imports, maps, and analysis belong to `geo`.

## Investigator workflow

Open a case and choose **Case tools → Vehicles**. Owners and editors can choose
**Add vehicle**, enter known details, and save. At least one make, model, license plate, or VIN is
required; partial details are allowed. Viewers can search and inspect records.
The list searches make, model, exact numeric year, color, plate, VIN, and saved notes,
with 25 records per page. Returning from a detail/edit screen preserves the list
query and page.

Choose **Edit vehicle** to correct reported details. If another save changed the
record, the form preserves the draft and displays all latest saved details.
**Save reviewed changes** explicitly resubmits it. A deleted record is never
recreated by a stale form. Case reassignment is not supported.

The detail page shows reported identifiers, locations, and an unverified-record
caption. **Record details** holds saved read-only notes, component fields,
timestamps, and the owner's **Delete vehicle** action.

Only the case owner can delete an individual vehicle after a separate named-target
confirmation. Retained vehicle-location observations, including archived evidence,
block individual deletion. Deleting an unlinked vehicle retains the case, people,
other vehicles, and login accounts. Whole-case deletion removes vehicles and their
observations together. Product users need no staff permissions.

The vehicle detail page lists up to 25 recent reported locations. Owners and
editors can add **Vehicle location** evidence or choose **Import vehicle
locations** to review an Asset Location CSV and explicitly map source assets to
case vehicles. Source identifiers never overwrite plates/VINs or establish
identity, ownership, or presence. See [Vehicle locations and CSV
imports](vehicle-location-imports.md) for formats, retention, limits, and recovery.

## Fields and validation

A PostgreSQL constraint requires a non-whitespace make, model, plate, or VIN,
including on bulk writes. Year, color, and notes alone are insufficient. A supplied
year must be from 1 through 9999; leave it blank when unknown. Partial or historical
identifiers are accepted and are not unique within or across cases. No
country-specific plate validation, VIN verification, normalization, ownership
lookup, or merging occurs.

Saved notes remain read-only. Timestamps and Django admin history are not a
versioned evidence or vehicle-assertion history. The [model](../vehicle/models.py)
owns field definitions and constraints; trusted ORM callers must use
`full_clean()` for field validation before saving.

## Django admin and authorization

Use **Vehicles** in `/admin/` under the [shared admin gates](cases.md#django-admin),
with `vehicle` model permissions. Only case owners with `vehicle.delete_vehicle`
can delete a vehicle, subject to retained-observation protection. Existing case,
notes, and timestamps are read-only; case reassignment is unsupported.

Product mutations recheck current case permissions under a case lock and require
CSRF-protected POST. Authorization is not automatic for ORM callers: exports,
jobs, and integrations must enforce the same case boundary themselves.

## Migration and scope

`vehicle` owns the vehicle table and constraints; `geo` owns vehicle-location
evidence and CSV import tables. Use the
[fresh migration baseline](architecture.md#primary-keys-and-the-initial-schema).

Person/vehicle relationships, external vehicle lookups, and public APIs remain
planned. Geographic analysis of retained vehicle locations is implemented in `geo`.

## Automatic trips

Vehicle details lead with five recent possible trips derived from retained timed
locations. Raw location records remain available under **Show observations**. **See all trips** or **Choose dates** opens the complete report selection.
CSV address-only imports work without geocoding or review. Stops and gaps separate
trips; each endpoint and contributing observation links to its source. Reports
inspect the latest 500 matching observations and disclose incomplete coverage;
use vehicle/date filters for older intervals. See [location analysis](analysis-locations.md).
