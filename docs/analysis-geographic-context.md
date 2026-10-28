# Automatic geographic context

**Look up geographic context** obtains road, venue, building and neighbourhood
candidates around selected location observations. It is separate from the local
**Group nearby observations** operation, which still needs no external service.
The same methods accept Location, Person location and Vehicle location evidence.
They do not assign unlinked observations to a person or identify a driver.

## From miTrail records to context

The current Asset Location CSV importer retains reported addresses and timestamps,
not coordinates. Select an address for the existing forward-geocoding operation,
review the particular candidate, and then select that accepted interpretation for
context lookup. Already retained coordinates can be used directly. Timestamps
alone cannot locate an observation or supply a road/building reference layer.
Investigators do not need to prepare or upload GeoJSON files.

Start an analysis, choose **Look up geographic context**, select observations or
accepted geocodes, and inspect the grouping distance and context-search radius.
Approve the disclosed external lookup before queuing it. The worker sends only
coordinate areas, fixed feature filters and the selected reference date. It sends
no case names, person identifiers, vehicle identifiers, original addresses or
observation timestamps. Authorization is rechecked immediately before and after
each request and again before publication; sent requests cannot be recalled.

## What a result means

Grouping reuses deterministic anchor-radius groups. Each context candidate links
to the particular member observation supporting it, rather than assigning one
place identity to an entire cluster. Results retain recorded versus reviewed
derived positions, observation times, map feature IDs, approximate distances,
polygon containment candidates, boundary ambiguity and overlapping alternatives.
Building multipolygons and their holes are handled separately from nearby roads
and point labels. A neighbourhood point is only a nearby label; it does not supply
an area boundary. Shared buildings do not establish shared occupancy, visits,
meetings, identity or a person's presence.

The default context-search radius is **500 m** (supported 1–5,000 m). The boundary
ambiguity band defaults to **10 m** (0–100 m), and grouping defaults to **100 m**.
A request covers the grouping anchor plus its farthest selected member and the
search radius. A combined radius above 5,000 m, a window reaching 85 degrees
latitude, or one crossing the dateline is excluded explicitly. Narrow the
selection or distance settings to recover. Distances use a small-window local
metric approximation; decimal precision and the ambiguity band do not establish
measured positional accuracy.

Local radius queries can miss large polygons whose enclosing boundaries lie
outside the query, and OpenStreetMap may contain incomplete or outdated features.
No returned match does not establish that no building, venue, road or neighbourhood
exists. Unsupported or invalid geometry, omitted responses and output limits stay
visible as partial coverage with counts. Full geometry remains in the retained
reference JSON rather than cluttering the result list.

## Current and historical reference dates

Without a historical timestamp, the result describes the retrieved current map
snapshot. Its dataset timestamp and retrieval time remain separate from the
observation time. Current context must not be read as a statement about historical
conditions.

An optional historical reference timestamp must include an explicit timezone,
use whole-second precision, and is normalized to UTC. The worker sends that exact
requested instant using
Overpass's date setting. Historical lookup requires an operator-declared history
start; unavailable history, service errors and malformed responses never trigger
a retry against current map data. Current generated-area indexes are not used as
substitutes for historical geometry. Even a historical map snapshot records map
data at that time rather than independently proving real-world conditions.

Every successful reference response retains its exact private bytes and SHA-256,
query, source selection, endpoint identity, source/version, attribution, retrieval
time, requested historical date and supplied dataset timestamp. Findings link the
snapshot and feature ID to the original observation. Reruns retain earlier
responses and reviews. Authorized viewers can download the reference JSON;
downloads verify current case/source access and the retained checksum.

## Operator configuration

Configure the same values on web and worker:

| Variable | Meaning |
| --- | --- |
| `PANDECT_MAP_CONTEXT_URL` | Full operator-provided Overpass interpreter endpoint, for example `https://maps.example.test/api/interpreter`. Empty disables lookup. |
| `PANDECT_MAP_CONTEXT_NAME` | Human-readable map source name. Required. |
| `PANDECT_MAP_CONTEXT_VERSION` | Operator's reference dataset/version identifier. Required. |
| `PANDECT_MAP_CONTEXT_ATTRIBUTION` | Required source attribution and licensing notice. |
| `PANDECT_MAP_CONTEXT_USER_AGENT` | Application/operator identification accepted by the service. Required. |
| `PANDECT_MAP_CONTEXT_TIMEOUT` | Request timeout in seconds; default 10, bounded to 1–30. |
| `PANDECT_MAP_CONTEXT_HISTORY_START` | Earliest available historic snapshot as `YYYY-MM-DD` in UTC or an explicitly zoned ISO timestamp. Empty means historical lookup is unavailable. |

No public endpoint is supplied as a default. Operators arrange an appropriately
provisioned service, dataset coverage, retention and licensing. The public
Overpass service's [operating guidance](https://dev.overpass-api.de/overpass-doc/en/preface/commons.html)
advises applications to operate suitable backend capacity. Preserve the supplied
attribution and [OpenStreetMap licensing notice](https://www.openstreetmap.org/copyright)
alongside retained reference results. Map lookup is not ML inference; it does not
use the AI Gateway or send content to a model.

## Bounds and validation

Each run permits at most **25 query areas**, **2 MiB per response**, **8 MiB of
retained responses**, **2,000 processed features**, **50,000 returned/assembled
vertices** and **1,000 displayed context matches**. Findings also have a 1 MiB
display-byte budget. Excluded observations, geometry/display truncations and
omitted findings are counted. The service client follows no redirects and makes
no automatic retries. Recover by narrowing the selection or starting a new run.

OSM relation assembly, polygon-hole validation and geometric matching run in the
existing isolated parser subprocess with its CPU, memory, output and wall-time
bounds. The child receives only response bytes, coordinates and bounded settings,
without application credentials, case records or storage paths. Snapshot creation
and result publication use the existing atomic workflow and rollback cleanup.

Synthetic tests exercise current/historical queries, point labels, roads,
overlapping polygons and holes, missing/invalid geometry, quotas, response and
display bounds, authorization changes and exact source retention. HTTP calls are
mocked; these checks do not establish live map-service availability, completeness
or positional accuracy.

Primary references: [Overpass historical queries](https://dev.overpass-api.de/overpass-doc/en/analysis/museum.html)
and [OSM geometry conversion](https://github.com/rapkin/osm2geojson).
