# Investigation map

**Case Tools → Trips** lets all case roles inspect processed vehicle trips and
follow retained evidence. Owners/editors also **Add POI**. `geo` owns views,
lookup coordination, POIs, templates, assets, and tests; `geo.map_views` serves
`geo:case_map`, `geo:prepare_trips`, and `geo:trip_detail` at their case-scoped URLs.

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

Search covers labels, types, addresses, citations, and person names/contacts.
The vehicle and date filters affect trips only. Independent URL-backed
`poi_page` lists at most 100 POIs per page; only that page is plotted alongside
the selected complete trip. Person details list 25 POIs per page and preselect
the person when adding.
POI details opened from a map marker or list return to the same map search and
pages. **Back to trip** reopens the selected trip with its map filters, source
page, and findings page. If that trip becomes unavailable, the link returns to
the filtered case map. From a Person record, **Back to person** returns to that
person's Points of interest section with its page and People list filters.
These origins carry through add, edit, and delete, including **Add POI here**
on the map. Cancelling or saving an edit returns to the point. Deleting a
point returns to the view it came from. If an edit changes the associated
person, the return link still leads to the Person record that opened the task.
Latitude and longitude sit together when space allows and stack on phones.

Owners/editors add/edit/delete; viewers inspect. Requests check current access
and restrict person selection server-side. Stale edits preserve drafts for review.
Deletion needs named confirmation and POST. Person/case deletion also removes
POIs; retained evidence still protects person deletion. POIs record creator and
creation/update times without versioned history, and have no admin editor.

`geo.PointOfInterest` uses a UUID table scoped through its required person, with
label/coordinate constraints in fresh `geo/0001_initial`. Use the [fresh baseline](architecture.md#primary-keys-and-the-initial-schema)
with a new database/queue; no old-table conversion is retained.

## Processed vehicle trips

Trips opens with the list of saved possible trips. Open one to inspect its
first and last observations, elapsed time, map, and chronological sources.
Trip findings cite one or more observations in the selected trip. The findings
cover the full trip, even when its observation list spans multiple pages.
Raw Location, Person location, and Vehicle
location observations remain in Evidence rather than the map's main display.
Editors can open vehicle CSV intake or add a vehicle observation from an empty map.

Expand **Search and filters** to filter active trips by vehicle and dates in
the displayed account/device timezone or to search POIs. Applied filters appear
as a short summary. The form opens automatically when a filter needs correction.
Dates select whole overlapping trips, never trim observations
before segmentation. Trip lists paginate at 25; details show 50 observations per
page with evidence/CSV-row links. Pagination never limits processing history.
See [trip rules](analysis-locations.md#automatic-vehicle-trips).

New manual observations, completed imports, archive/restore, and confirmed source
deletion invalidate affected vehicles' trips. A verified owner or editor's write
queues rebuilding. If a superuser with viewer-only case access deletes a source,
an owner or editor must [prepare replacement trips](background-tasks.md#saved-vehicle-trips).
Map, report, and vehicle details share the current completed generation.
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

Geometry arrives in cursor pages of at most 500 sources, progressively drawn on
one canvas. A trip made entirely of reported coordinates can reuse one complete
response in tab memory after a successful server recheck of that trip and its
generation. Address lookups elsewhere in the case leave these reported positions
unchanged. The map clears the previous overlay and shows loading during the
recheck. A changed revision triggers a fresh read. Trips with address estimates
or missing coordinates also fetch fresh positions on redraw. Larger trips
stream without retaining completed coordinates. Requests recheck case access
and the published generation. Fresh position reads resolve current address
selections.
Server-rendered trip/source lists work without JavaScript or tiles.

## Local address lookup

Address-only trips process without geocoding. After publication, separate background
preparation queues missing private-geocoder lookups in batches of 25 distinct
addresses, subject to the analysis source limit (at most 500). Batches continue
through eligible trip sources; this is no total-history cap. Failure does not
block trip summaries. Pending/terminal lookups remain retained; failed lookups
require deliberate retry from their Processing pages.

Set `PANDECT_GEOCODER_LOCAL=true`, private `PANDECT_GEOCODER_URL`, source name,
dataset version, attribution, and User-Agent on **both web and worker**. See
[configuration](analysis-locations.md#geocoding-configuration-and-estimate-selection). There is
no public default/cloud fallback; browsers never contact the geocoder. Workers
send selected original addresses only, excluding files, titles, case IDs, and
vehicle identifiers. Endpoint/version checks, authorization, bounds, and provenance apply.

Open **Review address estimates for this trip** to see address-only observations
together, then follow **Open address lookup** for an observation. Open a candidate and choose
**Use this estimate**. The map uses exactly one selected candidate from the latest
matching local lookup. Reported coordinates take precedence. Selecting or clearing
an estimate refreshes geometry and preserves original observations and segmentation. There is no global address cache.

## External tiles and privacy

Vendored Leaflet 1.9.4 lives in `geo/static/geo/vendor/leaflet/` with its BSD license;
templates live in `geo/templates/geo/`. Map/trip detail owns one map instance.
POIs use escaped `json_script`; trip positions use authorized JSON. Titles render
as DOM text, never popup HTML. Complete lists work without JavaScript, tiles, or Leaflet.

No basemap is configured by default. Configured tiles load on opening, without
consent, including external providers. Providers receive browser IP, site origin,
and viewed tile areas; files, titles, notes, addresses, case IDs, and observation
times are not sent. Marker details render locally; address lookup remains local.
Tile images use `referrerPolicy: origin`; other private pages keep same-origin
policy. Only successful map/trip pages permit the tile origin in image CSP.
Private HTML is uncached; tiles follow their server's cache headers.

The OSM background is orientation context, not retained evidence/history.
[Geographic context](analysis-geographic-context.md) is a separate explicit lookup
retaining responses/provenance.

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
uv run --locked --env-file .env python manage.py test --settings=config.test_settings geo.tests.test_map geo.tests.test_map_geocoding geo.tests.test_pois config.tests.test_product_access --noinput
uv run --locked --env-file .env python manage.py test --settings=config.test_settings geo.map_browser --noinput
```

Chromium/WebKit use synthetic trips/POIs and mocked tiles to check progressive
drawing, complete sources, estimate selection, keyboard/POI controls, no-JavaScript,
tile failure, desktop/mobile, and 320px reflow. Captures use ignored
`data/validation/maps`. Unit tests cover histories beyond 100/500 observations,
chunk boundaries, roles/revocation, case/source integrity, retries, stale generations,
and deletion inventories. They send no real addresses and establish neither live
provider availability nor full accessibility compliance.
