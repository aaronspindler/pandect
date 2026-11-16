# Investigation map

Open a case and choose **Case tools → Map**. Owners, editors and viewers can inspect
case-scoped processed vehicle trips and follow links back to retained evidence.
Owners and editors can choose **Add POI**. The `geo` app owns the map
views, local lookup coordination, POIs, templates, static assets, and tests.
`geo.map_views` serves the existing case-scoped URLs, with named routes
`geo:case_map`, `geo:prepare_trips`, and `geo:trip_detail`.

## Person points of interest

Choose **Add POI** to link a place to a person in the current case. Choose
**Home**, **Work**, or **Custom** and enter a custom label such as Gym. Enter
WGS84 latitude/longitude, with an optional address and source/citation.
Previously saved POI notes remain readable. Click or tap a spot on the map and
choose **Add POI here** to prefill coordinates before choosing the person and label.
The normal form works with a keyboard and without JavaScript or map tiles.
If the case has no people, the form offers **Add person** first.

POIs are editable investigator annotations, separate from retained location
evidence. Labels do not verify residence, employment, ownership, or presence.
They do not enter evidence analysis or change a person's contact address.
An address is optional context: POIs require coordinates and do not start
address lookups. No new external service or inference route is used.

Diamond markers labelled **P1**, **P2**, and so on match the **Points of interest**
list. Popups show the label and person and link to the POI details. Identical
coordinates share a marker whose popup lists the POIs there.
**Show on map** provides the keyboard alternative to marker selection. Polar
coordinates beyond the map display limit remain in the list without moving them.

Search includes POI labels, types, addresses, citations, and person names or
contacts. Vehicle, date and evidence-state filters affect only trips. The POI list
has its own URL-backed `poi_page`, at most 100 records per page; only that page
is plotted alongside the selected complete trip. Person detail pages also
list their POIs, 25 per page, with an **Add POI** link that prefills the person.

Current owners/editors can add, edit, and delete POIs; viewers can inspect them.
Every request checks case access and person selection is restricted server-side.
Concurrent edits preserve the draft and require review of newer saved details.
Deletion requires a separate named confirmation and POST. Deleting a person or
case also removes its POIs; retained evidence still enforces its existing person
deletion restrictions. POIs record their creator and creation/update times, but
do not provide a versioned annotation history. POIs have no Django admin editor.

`geo.PointOfInterest` is included in the fresh `geo/0001_initial` migration.
Its UUID table is scoped through the required person relationship, with database
constraints for labels and coordinate ranges. Use the
[fresh migration baseline](architecture.md#primary-keys-and-the-initial-schema)
on a new database and queue; no old POI table or data conversion is retained.

## Processed vehicle trips

The map shows saved possible vehicle trips, with the latest matching trip selected
initially. Choose a trip to inspect its complete path and contributing observations.
Raw Location, Person location and Vehicle location evidence is available through
Evidence; individual observations are no longer plotted as the map's main content.

Filter by vehicle, active/archived/all evidence, and dates in the displayed account
or device timezone. Date filters select whole trips overlapping the interval;
they do not cut observations before segmentation. The summary list has 25 trips
per page. Trip detail lists 50 source observations per page, with original evidence
and imported CSV row links. List pagination never limits the history used to
process a trip. See [trip rules](analysis-locations.md#automatic-vehicle-trips).

New manual observations, completed imports, archive/restore changes and confirmed
source deletion invalidate the affected vehicle's saved trips and queue rebuilding.
The map, report and vehicle page use the same current completed generation. Pending,
failed and superseded output is not displayed as current. Existing cases without
saved trips offer **Prepare trips** to owners/editors; JavaScript submits this
idempotent POST automatically. Viewers cannot start processing. GET requests do
not enqueue jobs. Follow progress and recovery in **Processing**.

Lines connect reported positions or specifically reviewed address estimates in time
order. Missing or ambiguous positions interrupt the line. The map draws no raw
observation markers and reconstructs no road route. A trip remains in the list
when it has no drawable positions. First/last observed places and times are not
verified departure or arrival, continuous presence, driver identity, or distance
travelled. Polar positions beyond Web Mercator's ±85.05112878° range remain in
source inspection without clamping the original.

Geometry arrives in cursor pages of at most 500 sources and is progressively drawn
onto one canvas. Completed coordinate buffers are discarded; pan/zoom replays the
selected trip. Loading status distinguishes a partial drawing from a completed
one. Every request rechecks current case access and the published generation;
changed address reviews stop an obsolete replay. The server-rendered trip and
source lists remain available without JavaScript or tiles.

## Local address lookup

Address-only trips are processed without geocoding. After trip publication, a
separate background preparation step queues missing lookups through the configured
private geocoder, in batches of at most 25 distinct addresses and the operator's
analysis source limit (at most 500 sources). Batches continue through the eligible
trip sources; this is not a total-history limit. Lookup failure does not block trip
summaries. Existing pending and terminal lookups are retained; failed lookups are
retried deliberately from their existing processing pages.

Configure `PANDECT_GEOCODER_LOCAL=true` together with the private
`PANDECT_GEOCODER_URL`, source name, dataset version, attribution and User-Agent
on **both web and worker**. See [geocoding configuration](analysis-locations.md#geocoding-configuration-and-review).
There is no public geocoder default or cloud fallback. The browser never contacts
the geocoder. Workers send only selected original addresses, without evidence
files, titles, case IDs or vehicle identifiers. Existing endpoint/version checks,
case authorization, resource limits and retained lookup provenance apply.

Open a trip's observations and follow **Review lookup** to inspect candidates.
Only a specifically accepted candidate from the latest matching local lookup can
supply an estimated path position. Whole-run approval does not choose a candidate;
multiple accepted alternatives remain unmapped. Reported coordinates take
precedence. Rejection or changed acceptance refreshes geometry without changing
the trip's original-address/coordinate segmentation. Original addresses,
coordinates and checksums remain unchanged. No global address cache is introduced.

## External tiles and privacy

Leaflet 1.9.4 is vendored under `geo/static/geo/vendor/leaflet/` with its BSD
license. Map templates live under `geo/templates/geo/`. One map instance belongs
to the map or trip-detail page; POIs use Django's escaped `json_script` and
trip positions use authorized JSON responses.
Titles are rendered as DOM text, never as popup HTML. The complete server-rendered
list works without JavaScript, tiles, or the map library.

No basemap is configured by default. Configured tiles load when the map opens,
without a consent step, including tiles from an external provider.
The provider receives the browser IP address, site origin, and viewed tile
areas. Evidence files, titles, notes, source addresses, case IDs and observation
times are not sent to the tile provider. Address lookup is confined to the local
geocoder described above. Marker details are rendered locally in the browser. Tile images
use `referrerPolicy: origin`; ordinary private pages retain the existing
same-origin referrer policy. Only successful map and trip-detail pages' CSP permit images
from the configured tile origin. Private HTML remains uncached, while the browser
honors the tile server's normal caching headers.

The current OSM background is orientation context, not retained evidence or a
historical snapshot. [Geographic context analysis](analysis-geographic-context.md)
is a separate explicit lookup with retained source responses and provenance.

Keyboard users can pan with arrow keys, zoom with +/− or 44px zoom controls,
inspect markers, and use the list and source links. Mouse-wheel zoom is disabled
to preserve document scrolling. Touch panning/pinch zoom follow Leaflet behavior;
the list stays outside the map's gesture area. Tile failures leave the trip paths
and POIs and list usable and explain how to retry.

## Operator configuration

| Variable | Default and behavior |
| --- | --- |
| `PANDECT_MAP_TILE_URL` | Empty by default: disables background tiles while retaining trip paths and POIs. For OpenStreetMap, set `https://tile.openstreetmap.org/{z}/{x}/{y}.png`. A configured URL must be an HTTPS raster tile template containing `{z}`, `{x}`, `{y}`, without credentials, query or fragment. It is visible to the browser; never put secrets in it. |
| `PANDECT_MAP_TILE_ATTRIBUTION` | Optional additional plain-text provider attribution. OpenStreetMap copyright attribution always remains visible. |

Compose passes these variables to the shared application environment. For hosted
or high-volume use, arrange a suitable OSM-derived provider or operate a tile
service. Review the [OSMF tile policy](https://operations.osmfoundation.org/policies/tiles/):
public tiles have no availability guarantee, require visible attribution and a
valid browser referrer, and prohibit bulk/offline downloading. Pandect requests
only viewed tiles, with no prefetch, bulk download, automatic retry loop or
cache-busting. Browser validation mocks tile requests rather than using public
capacity.

## Validation

Use the documented local PostgreSQL test setup:

```sh
uv run --locked --env-file .env python manage.py test --settings=config.test_settings geo.tests.test_map geo.tests.test_map_geocoding geo.tests.test_pois config.tests.test_product_access --noinput
uv run --locked --env-file .env python manage.py test --settings=config.test_settings geo.map_browser --noinput
```

The browser journey uses synthetic trips/POIs and mocked tile responses in
Chromium and WebKit. It checks progressive trip drawing, complete source access,
local candidate review, keyboard controls, POI placement, no-JavaScript fallback,
tile failure, desktop/mobile layouts and 320px reflow. Captures are saved under
ignored `data/validation/maps`. Unit tests cover complete processing beyond 100
and 500 observations, chunk boundaries, current roles, revocation, case/source
integrity, retries, stale generations and deletion inventories. Routine tests
send no real addresses and do not establish live provider availability or full
accessibility compliance.
