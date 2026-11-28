# Trips and the investigation map

**Case Tools → Trips** brings saved possible trips, location coverage gaps, and
the map together for all case roles. Owners and editors can also **Add POI**.
Existing Vehicle trips report links redirect here with supported filters.

## Person points of interest

**Add POI** links a same-case person to **Home**, **Work**, or **Custom** (for
example Gym). Enter WGS84 latitude/longitude, optionally address/source citation.
Saved notes remain readable. **Add POI here** prefills a clicked/tapped position
before person/label selection. The form works by keyboard, without JavaScript
or tiles; cases without people offer **Add person** first.

POIs are editable investigator assertions, separate from evidence/contact
addresses. Labels verify no residence, employment, ownership, or presence. POIs
require coordinates, never trigger address lookups, and do not enter evidence
analysis. They introduce no external service/inference route.

Diamond markers **P1**, **P2**, etc. match **Points of interest**. Popups show
label/person and link to details; identical positions share a marker listing all
POIs. **Show on map** offers keyboard selection. Polar positions beyond display
limits remain in the list without coordinate changes.

**Search POIs** uses [hosted search](cloudflare-search.md) for POI fields such
as label, address and source. Clear the query to browse if search is unavailable.
The map plots the current page of up to 100 POIs alongside the trip. Person
details list 25 POIs per page and preselect the person when adding.

POI actions preserve the originating map, trip or person and its filters and
pages. **Back to trip** falls back to Trips if the selected trip becomes
unavailable. Changing the POI's person keeps the return link to its original
person context.

Owners/editors add/edit/delete; viewers inspect. Requests check current access
and restrict person selection server-side. Stale edits preserve drafts for review.
Deletion needs named confirmation and POST. Person/case deletion also removes
POIs; retained evidence still protects person deletion. POIs record creator and
creation/update times without versioned history, and have no admin editor.

## Processed vehicle trips

Trips opens with saved possible trips and source links for their first and last
observations. Open a trip to see its map and chronological sources.
Trip findings cite one or more observations in the selected trip. The findings
cover the full trip, even when its observation list spans multiple pages.
Raw Location, Person location, and Vehicle
location observations remain in Evidence rather than the map's main display.
Editors can open vehicle CSV intake or add a vehicle observation from an empty map.

The **Vehicle** and **Jump to date** controls stay visible above the trip list.
Select a vehicle and use **Go** to see its history. Choose a date in the displayed
timezone and use **Go** to jump to the first whole trip that overlaps that day.
The map previews that trip, and the full history for the selected vehicle or all
vehicles stays available in reverse chronological order. **Clear date** keeps
the vehicle selection.

Trip lists and coverage gaps paginate independently at 25 per page. Trip details
show 50 observations per page with evidence and CSV-row links. Processing uses
complete vehicle histories. See [trip rules](analysis-locations.md#automatic-vehicle-trips).

New manual observations, completed imports, archive/restore, and confirmed source
deletion invalidate affected vehicles' trips. A verified owner or editor's write
queues rebuilding. If a superuser with viewer-only case access deletes a source,
an owner or editor must [prepare replacement trips](background-tasks.md#saved-vehicle-trips).
Trips and vehicle details share the current completed generation.
Cases lacking trips offer **Prepare trips** to owners and editors. JavaScript
submits this idempotent POST automatically for eligible users. GET never
enqueues work. Processing shows progress and recovery.

Lines connect reported positions or selected address estimates in
time order, interrupted by missing or ambiguous positions. Arrows follow
observation order. Color and line pattern show the speed reported at the next
observation. The key distinguishes under 30, 30 to under 60, 60 km/h or more, and
unavailable speed. The bands represent point-in-time readings. Speed between
observations remains unknown. **Trip observations** lists exact reported speeds,
including zero, and identifies unavailable readings. Concise context and
tile-provider privacy details stay visible beside the map. **Map details**
retains interpretation, lookup, and keyboard help.
Trips without drawable positions remain listed.
First/last observed places/times verify no departure/arrival, continuous presence,
driver, or travelled distance. Positions beyond Web Mercator ±85.05112878° remain
inspectable without clamping originals.

Geometry streams in pages of up to 500 sources. Requests recheck current case
access and trip generation. The map can reuse complete reported-coordinate data
after rechecking its revision. Address estimates are read fresh. Trip and source
lists work without JavaScript or tiles.

## Local address lookup

Address-only trips process without geocoding. After publication, the worker
can queue missing private-geocoder lookups in batches of 25 distinct addresses.
Batches continue across eligible trip history. Failed lookups have a deliberate
retry in Processing and leave trip summaries available.

Configure the private geocoder on web and worker. The worker sends only the
selected original addresses and has no public fallback. See [geocoder
configuration](analysis-locations.md#geocoding-configuration-and-estimate-selection).

Open **Review address estimates for this trip** to see address-only observations
together, then follow **Open address lookup** for an observation. Open a candidate and choose
**Use this estimate**. The map uses exactly one selected candidate from the latest
matching local lookup. Reported coordinates take precedence. Selecting or clearing
an estimate refreshes geometry and preserves original observations and segmentation. There is no global address cache.

## Observation maps in findings

**Selected observation positions** loads the configured background map with
the displayed finding or page of results. Circles show reported positions,
squares show selected address estimates, and diamonds show lookup candidates.
Markers link to the source observation or retained estimate. The background
shows geographic context for orientation. Candidate status and retained
coordinates still govern interpretation.

**Coordinates and sources** works without JavaScript or tiles and includes
positions outside Web Mercator's latitude range. If the interactive map cannot
start, distinct coordinates also appear in a relative position plot.

## External tiles and privacy

The locally served [Leaflet build and license](../geo/static/geo/vendor/leaflet/README.md)
render maps. No basemap is configured by default. Opening Trips or an
observation map with configured tiles sends the tile provider the browser IP,
site origin and viewed tile areas. Marker details and evidence render within
Pandect. Tile images use origin-only referrers and follow the provider's cache
headers.

Basemaps provide orientation. Use [Geographic context](analysis-geographic-context.md)
for explicit lookups with retained responses and provenance.

Keyboard users pan with arrows, zoom with +/− or 44px controls, inspect markers,
and use lists/source links. Wheel zoom is disabled for document scrolling; touch
uses Leaflet pan/pinch behavior. Lists stay outside map gestures. Tile failures
explain retry and leave paths, POIs, and lists usable.

## Operator configuration

| Variable | Default and behavior |
| --- | --- |
| `PANDECT_MAP_TILE_URL` | Empty disables background tiles, retaining paths/POIs. OpenStreetMap: `https://tile.openstreetmap.org/{z}/{x}/{y}.png`. Must be an HTTPS raster template with `{z}`, `{x}`, `{y}` and no credentials, query, or fragment. Browser-visible: never include secrets. |
| `PANDECT_MAP_TILE_ATTRIBUTION` | Optional plain-text provider attribution; OpenStreetMap copyright remains visible. |

Compose passes both to the shared application environment. Hosted/high-volume use
needs a suitable provider or private tile service. [OSMF policy](https://operations.osmfoundation.org/policies/tiles/)
requires attribution/valid browser referrer, prohibits bulk/offline downloads, and
offers no availability guarantee. Pandect requests viewed tiles only, without
prefetch, bulk download, retry loops, or cache-busting. Browser tests mock tiles.

## Validation

Use the documented local PostgreSQL setup:

```sh
uv run --locked --env-file .env python manage.py test --settings=config.tests.settings geo.tests.test_map geo.tests.test_map_geocoding geo.tests.test_pois config.tests.test_product_access --noinput
uv run --locked --env-file .env python manage.py test --settings=config.tests.settings geo.tests.map_browser --noinput
```

The browser journeys use synthetic trips and POIs with mocked tiles. Cover
keyboard controls, reflow, unavailable tiles, estimate selection, source
navigation, and the no-JavaScript list. Keep captures under ignored
`data/validation/maps/`.
