# Vehicles

`vehicle.Vehicle` is an investigator-maintained record in exactly one `cases.Case`.
Reported plates/VINs do not establish identity, ownership, or association with a
person. Records are not automatically linked to people or vehicles; investigators
can link location evidence explicitly to a same-case vehicle. `vehicle` owns entity
models, forms, views, templates, admin, and tests; `geo` owns locations, imports,
maps, and analysis.

## Investigator workflow

Open **Case tools → Vehicles**. Owners/editors choose **Add vehicle**, enter at
least one make, model, plate, or VIN, and save; partial details are allowed.
Viewers search/inspect. Search covers make, model, exact numeric year, color,
plate, VIN, and saved notes, with 25 records per page. Search and page context
survives adding, editing, deleting, and returning from records.

**Edit vehicle** corrects reported details. Stale forms preserve the draft beside
all current details and require **Save reviewed changes**; deleted records cannot
be recreated. Case reassignment is unsupported.

Details show reported identifiers, locations, and an unverified-record caption.
**Record details** holds saved read-only notes, component fields, and timestamps.
Owner-only **Delete vehicle** is visible below that disclosure and opens the
separate confirmation.

Only owners delete individual vehicles after named-target confirmation. Retained
vehicle locations, including archived observations, and CSV imports that reference
the vehicle block deletion. The deletion page links to the retained records and
omits the confirmation when blocked. Deleting an unlinked vehicle retains its
case, people, other vehicles, and login accounts;
whole-case deletion removes vehicles and observations together. Product users
need no staff permissions.

Details paginate active locations at 25 per page.
Owners/editors have **Add a location** and **Import vehicle locations** beside
**Edit vehicle** in the header, including before any trips exist. Both actions
suggest the current case vehicle. Imports review an Asset Location CSV and
map assets explicitly. Source identifiers
never overwrite plates/VINs or verify identity, ownership, or presence. See
[CSV imports](vehicle-location-imports.md) for formats, retention, limits, and recovery.

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

Product mutations recheck case permissions under a case lock and require
CSRF-protected POST. ORM callers, exports, jobs, and integrations must enforce
the case boundary themselves.

## Migration and scope

`vehicle` owns the entity table/constraints; `geo` owns vehicle-location evidence
and imports. Use the [fresh migration baseline](architecture.md#primary-keys-and-the-initial-schema).

Person/vehicle relationships, external lookups, and public APIs remain planned.
`geo` implements analysis of retained vehicle locations.

## Automatic trips

Details show the latest dated active location, its time, and a link to the
evidence. **Show observations** lists active reported locations,
including undated records. Below the locations, the vehicle shows up to five
recent possible trips. **See all trips** opens Trips when the vehicle has more
than five. **Jump to date** opens the date control. Both links preselect the
vehicle. Each trip links to its paginated source observations. See
[Trips and the investigation map](maps.md).

New evidence, completed imports, and archive/restore changes queue rebuilding.
Pending/failed generations are not current; Processing provides progress/recovery.
Address-only records form trips without geocoding; map positions require reported
coordinates or specifically reviewed local estimates. Stops/gaps separate trips
without reconstructing roads or verifying presence. See [trip processing](analysis-locations.md#automatic-vehicle-trips).
