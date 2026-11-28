# Vehicles

Vehicles are investigator-maintained case records. Reported plates and VINs
establish neither ownership nor identity. Investigators can link location
evidence explicitly to a vehicle in the same case.

## Investigator workflow

Open **Case Tools → Vehicles**. Owners and editors can **Add vehicle** with at
least one make, model, plate or VIN. All case members can browse 25 records
per page and use [hosted search](cloudflare-search.md) when enabled. Return links
retain the search and page during record changes.

**Edit vehicle** corrects reported details. Stale forms preserve the draft beside
all current details and require **Save reviewed changes**; deleted records cannot
be recreated. Case reassignment is unsupported.

Details show reported identifiers, locations, and an unverified-record caption.
**Record details** holds saved read-only notes, component fields, and timestamps.
Owner-only **Delete vehicle** is visible below that disclosure and opens the
separate confirmation.

Only owners can delete individual vehicles after named confirmation. Retained
locations, including archived observations, and CSV imports that reference the
vehicle block deletion. The deletion page links to those records. Whole-case
deletion removes vehicles and observations together.

Details paginate active locations at 25 per page. Owners and editors have
**Add a location** and **Import vehicle locations** beside **Edit vehicle**.
Both preselect the current vehicle. Review CSV asset associations explicitly.
See [CSV imports](vehicle-location-imports.md) for formats, limits and recovery.

## Fields and validation

PostgreSQL requires a non-whitespace make, model, plate, or VIN, including bulk
writes; year/color/notes alone are insufficient. Optional years range from 1–9999.
Partial/historical identifiers are accepted and need not be unique within or
across cases. There is no country-specific plate validation, VIN verification,
normalization, ownership lookup, or merging.

Notes remain read-only. Timestamps/admin history are not versioned evidence or
vehicle-assertion history. The [model](../vehicle/models.py) owns fields/constraints;
trusted ORM callers must call `full_clean()` before saving for field validation.

## Django admin and authorization

Use `/admin/` → **Vehicles** under the [shared gates](cases.md#django-admin) and
`vehicle` permissions. Deletion requires ownership and `vehicle.delete_vehicle`.
Retained observations and CSV references block deletion. Existing case, notes,
and timestamps are read-only.

Jobs, exports and other ORM callers enforce current case access themselves.

## Migration and scope

`vehicle` owns the entity table. `geo` owns location evidence, imports, maps and
location analysis. Use the [fresh migration
baseline](architecture.md#primary-keys-and-the-initial-schema).

## Automatic trips

Details show the latest dated active location, its time, and a link to the
evidence. **Show observations** lists active reported locations,
including undated records. Below the locations, the vehicle shows up to five
recent possible trips. **See all trips** opens Trips when the vehicle has more
than five. **Jump to date** opens the date control. Both links preselect the
vehicle. Each trip links to its paginated source observations. See
[Trips and the investigation map](maps.md).

Processing shows rebuild progress and recovery. See [trip preparation and
map interpretation](maps.md#processed-vehicle-trips).
