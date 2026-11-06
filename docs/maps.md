# Investigation map

Open a case and choose **Map**. Owners, editors and viewers can inspect the same
case-scoped locations and follow links back to retained evidence. Owners and
editors can choose **Add location** or **Add POI**.

## Person points of interest

Choose **Add POI** to link a place to a person in the current case. Choose
**Home**, **Work**, or **Custom** and enter a custom label such as Gym. Enter
WGS84 latitude/longitude, with an optional address, source/citation, and
investigator notes. Alternatively, click or tap a spot on the map and choose
**Add POI here** to prefill coordinates before choosing the person and label.
The normal form works with a keyboard and without JavaScript or map tiles.
If the case has no people, the form offers **Add person** first.

POIs are editable investigator annotations, separate from retained location
evidence. Labels do not verify residence, employment, ownership, or presence.
They do not enter evidence analysis or change a person's contact address.
An address is optional context: POIs require coordinates and do not start
address lookups. No new external service or inference route is used.

Diamond markers labelled **P1**, **P2**, and so on match the **Points of interest**
list. Popups show the label and person and link to the POI details. Identical
coordinates share a marker whose popup lists all POIs and observations there.
**Show on map** provides the keyboard alternative to marker selection. Polar
coordinates beyond the map display limit remain in the list without moving them.

Search includes POI labels, types, addresses, citations, and person names or
contacts. Evidence type/status filters affect only observations. The POI list
has its own URL-backed `poi_page`, at most 100 records per page; only that page
is plotted alongside the current observation page. Person detail pages also
list their POIs, 25 per page, with an **Add POI** link that prefills the person.

Current owners/editors can add, edit, and delete POIs; viewers can inspect them.
Every request checks case access and person selection is restricted server-side.
Concurrent edits preserve the draft and require review of newer saved details.
Deletion requires a separate named confirmation and POST. Deleting a person or
case also removes its POIs; retained evidence still enforces its existing person
deletion restrictions. POIs record their creator and creation/update times, but
do not provide a versioned annotation history. POIs have no Django admin editor.

Apply the additive `people/0002_pointofinterest` migration using the
[documented migration workflow](development.md#run-python-on-the-host).
It creates a UUID table scoped through the required person relationship, with
database constraints for labels and coordinate ranges. Existing evidence and
person addresses are unchanged; no data conversion is required.

## Location observations

The map plots recorded WGS84 latitude/longitude from Location, Person location
and Vehicle location evidence. Numbered markers match the accessible location
list. A count groups observations with identical coordinates; its popup links
every source without moving the positions. Nearby markers can still overlap;
use the list's **Show on map** action to inspect each source. No route, identity,
ownership or presence is inferred. Observation times and source citations remain
in the list and originals remain unchanged.

The default view includes active observations. Search titles, addresses and source
citations, filter by evidence type or include archived material. Filters and page
are in the URL; each page contains at most 100 observations, newest additions
first. Only the current page is plotted. The map and its configured background
tiles load automatically when the page opens. Zoom/pan last for the current page.
**Search and filters** keeps search, type and archive controls together in one
native disclosure. It starts closed so the map is prominent, and opens when a
query or nondefault filter is active. **Apply filters** submits the current
selection with or without JavaScript. The map toolbar shows
mapped coverage and links to available lists. An empty POI list is omitted unless
a search is active; **Add POI** remains available in the page header.
The compact **Map details** disclosure contains keyboard help, source limitations
and the evidence privacy explanation, along with POI placement and list-filter
guidance.

Address-only observations use the configured **local** geocoder. When an owner
or editor opens a map page, JavaScript queues missing address lookups and updates
the map without moving keyboard focus. Viewers can inspect retained estimates
and follow in-progress work; they cannot start processing. Without JavaScript,
**Look up addresses locally** queues work and a normal refresh displays it.

Square markers distinguish address estimates from recorded-coordinate circles.
All currently unrejected candidates from the latest local lookup remain visible;
an ambiguous address is never silently assigned its first match. **Show on map**
fits all candidates for that observation. Each estimate links its source and
the retained lookup/review, with dataset attribution. Accepted estimates are
labelled reviewed; later rejection removes that candidate on the next read.
Inspecting an unreviewed candidate on this map does not accept it for downstream
distance, movement or context analysis; those still require candidate review.
Original addresses, reported coordinates and checksums remain unchanged.

Coordinates beyond Web Mercator's ±85.05112878° latitude limit stay in the list
with an explanation, without clamping the original position.

## Local address lookup

Configure `PANDECT_GEOCODER_LOCAL=true` together with the private
`PANDECT_GEOCODER_URL`, source name, dataset version, attribution and User-Agent
on **both web and worker**. See [geocoding configuration](analysis-locations.md#geocoding-configuration-and-review).
There is no public geocoder default or external fallback. An unconfigured,
non-local or unsupported endpoint blocks map lookups before queuing; a queued
endpoint/locality/version change fails before dispatch. A local designation is
an operator assertion of control, not an inference from the hostname. The browser
never contacts the geocoder directly. Only the selected original address is sent
by the worker to that service, with fixed lookup options; files, titles, notes,
case IDs and person/vehicle identifiers are excluded. Redirects and ambient HTTP
proxies are disabled by the shared geocoder client.

Local address lookup reuses the existing case-authorized analysis queue and retained
results; it adds no geocoding tables, global address cache or in-request HTTP lookups.
Every job rechecks current editor access, source case/relationships, immutable
checksums and the pinned local endpoint before requests and publication. The
same case lock and atomic enqueue prevent duplicate page submissions from
starting the same pending sources twice. Completed local analysis geocodes can
also be displayed; external or unpinned legacy results are not adopted.

One job handles up to 25 distinct addresses within the operator's analysis source
limit, deduplicated within its selected sources. A page has at most 100 observations and the browser starts at most four
new batches, with up to 200 status checks three seconds apart. Polling updates
only the current authorized page. Lookup failures and no matches stop automatic
retries; use **Look up addresses locally** to start another retained attempt.
If records or their details change while lookups run, updates stop and ask for a
refresh rather than attaching new marker numbers to a stale location list.
Addresses over 2,000 characters remain unsupported. Each lookup retains up to
five candidates; original coordinates always take precedence. If a worker is
stopped, work stays queued. For an abandoned running job, confirm the old worker
has stopped and cancel it from its lookup page before retrying. Earlier results
and reviews remain retained; see [background recovery](background-tasks.md).

## External tiles and privacy

Leaflet 1.9.4 is vendored locally with its BSD license. One map instance belongs
to the case page; evidence is supplied through Django's escaped `json_script`.
Titles are rendered as DOM text, never as popup HTML. The complete server-rendered
list works without JavaScript, tiles, or the map library.

No basemap is configured by default. Configured tiles load when the map opens,
without a consent step, including tiles from an external provider.
The provider receives the browser IP address, site origin, and viewed tile
areas. Evidence files, titles, notes, source addresses, case IDs and observation
times are not sent to the tile provider. Address lookup is confined to the local
geocoder described above. Marker details are rendered locally in the browser. Tile images
use `referrerPolicy: origin`; ordinary private pages retain the existing
same-origin referrer policy. Only the successful map page's CSP permits images
from the configured tile origin. Private HTML remains uncached, while the browser
honors the tile server's normal caching headers.

The current OSM background is orientation context, not retained evidence or a
historical snapshot. [Geographic context analysis](analysis-geographic-context.md)
is a separate explicit lookup with retained source responses and provenance.

Keyboard users can pan with arrow keys, zoom with +/− or 44px zoom controls,
inspect markers, and use the list and source links. Mouse-wheel zoom is disabled
to preserve document scrolling. Touch panning/pinch zoom follow Leaflet behavior;
the list stays outside the map's gesture area. Tile failures leave the source
markers and list usable and explain how to retry.

## Operator configuration

| Variable | Default and behavior |
| --- | --- |
| `PANDECT_MAP_TILE_URL` | Empty by default: disables background tiles while retaining source markers. For OpenStreetMap, set `https://tile.openstreetmap.org/{z}/{x}/{y}.png`. A configured URL must be an HTTPS raster tile template containing `{z}`, `{x}`, `{y}`, without credentials, query or fragment. It is visible to the browser; never put secrets in it. |
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
uv run --locked --env-file .env python manage.py test --settings=config.test_settings cases.tests.test_map cases.tests.test_map_geocoding config.tests.test_product_access --noinput
uv run --locked --env-file .env python manage.py test --settings=config.test_settings cases.map_browser --noinput
```

The browser journey uses synthetic locations and mocked tile responses in
Chromium and WebKit. It checks automatic tile loading, disabled basemaps, and
requests containing only tile paths without evidence payloads, source links, keyboard
controls, overlapping points, no-JavaScript fallback, tile failure, desktop/mobile
layouts, dark/forced-color/reduced-motion appearance and 320px reflow. Captures
also cover address estimates appearing after a mocked local background lookup,
ambiguous candidates, review links and keyboard source selection. Unit checks
cover local-only dispatch, configuration drift, revoked/cross-case access,
duplicate/bounded batches, review changes, and unchanged originals. No real
addresses are sent in tests. Captures are saved under ignored
`data/validation/maps`. These checks do not establish
live tile-provider availability or full accessibility compliance.
