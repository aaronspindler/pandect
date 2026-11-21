# Geographic context lookup

**Look up geographic context** finds road, venue, building and neighbourhood
candidates around Location, Person location or Vehicle location observations.
New eligible observations queue the configured lookup. Investigators can also
select sources and change lookup parameters under **Choose analysis**.
It does not assign unlinked observations to people or identify drivers. The
separate local **Group nearby observations** method needs no reference service.

## From miTrail records to context

Asset Location CSV imports retain addresses and timestamps, not coordinates.
Forward-geocode an address, choose **Use this estimate** on a candidate, then select that
interpretation for context lookup. Retained coordinates work directly; timestamps
alone supply neither location nor reference geometry. No GeoJSON upload is needed.

Choose **Look up geographic context**, observations or selected estimates, grouping
distance and search radius. External lookup requires case-owner approval in Case Settings.
`PANDECT_MAP_CONTEXT_LOCAL=true` explicitly designates operator control; a private
hostname alone does not. Requests contain coordinate areas, fixed feature filters
and reference date, excluding case/person/vehicle identifiers, original addresses
and observation times. Access is rechecked before/after requests and before
publication; submitted requests cannot be recalled.

## What a result means

Deterministic anchor-radius groups link each candidate to its supporting member
observation, without assigning one place identity to the cluster. Results retain
reported and estimated positions, observation times, feature IDs, approximate distances,
containment candidates, boundary ambiguity and overlapping alternatives. Building
multipolygons and holes are distinct from roads/point labels. A neighbourhood
point supplies no boundary. Shared buildings prove no occupancy, visit, meeting,
identity or presence.

Defaults are **500 m** search radius (1–5,000), **10 m** boundary ambiguity band
(0–100) and **100 m** grouping distance. Queries cover the anchor, farthest member
and search radius. Combined radii above 5,000 m, windows reaching 85° latitude or
crossing the dateline are excluded; narrow the selection/settings to recover.
Small-window local metric approximations and decimal precision do not establish
positional accuracy.

Radius queries can miss large polygons whose boundaries lie outside the query;
OpenStreetMap can be incomplete/outdated. No match does not establish that a feature is absent. Invalid or
unsupported geometry, omitted responses and limits produce counted partial
coverage. Full geometry remains in retained reference JSON.

## Current and historical reference dates

Without a historical timestamp, results describe the retrieved current snapshot.
Dataset, retrieval and observation times remain distinct; current context does
not establish historical conditions.

The historical snapshot field reads whole-second input in UTC regardless of your
account or device timezone. Pandect sends the exact UTC timestamp through
Overpass's date setting. The operator must declare
a history start. Missing history, errors or malformed responses never fall back
to current data or generated-area indexes. Historical snapshots describe map data,
not independently verified real-world conditions.

Each successful response retains exact private bytes/SHA-256, query, selection,
endpoint identity, source/version, attribution, retrieval time, requested date and
supplied dataset timestamp. Findings link snapshot/feature IDs to observations;
reruns preserve responses and the exact estimates used. Viewer downloads verify current case/source
access and checksum.

## Operator configuration

Use identical web/worker settings:

| Variable | Meaning |
| --- | --- |
| `PANDECT_MAP_CONTEXT_URL` | Full Overpass interpreter endpoint, e.g. `https://maps.example.test/api/interpreter`. Empty disables lookup. |
| `PANDECT_MAP_CONTEXT_LOCAL` | Default `false`; set `true` only for an operator-controlled service. |
| `PANDECT_MAP_CONTEXT_NAME` | Required human-readable source name. |
| `PANDECT_MAP_CONTEXT_VERSION` | Required dataset/version identifier. |
| `PANDECT_MAP_CONTEXT_ATTRIBUTION` | Required attribution and licensing notice. |
| `PANDECT_MAP_CONTEXT_USER_AGENT` | Required application/operator identification accepted by the service. |
| `PANDECT_MAP_CONTEXT_HISTORY_START` | Earliest snapshot: UTC `YYYY-MM-DD` or explicitly zoned ISO timestamp. Empty disables historical lookup. |

Requests have a fixed ten-second timeout. Service identification defaults to
`Pandect/0.1`; override it when the operator requires different contact details.

No public endpoint is configured by default. Operators arrange capacity, coverage,
retention and licensing; see public Overpass's
[operating guidance](https://dev.overpass-api.de/overpass-doc/en/preface/commons.html).
Retain attribution and the [OSM licensing notice](https://www.openstreetmap.org/copyright)
with results. This lookup uses neither AI Gateway nor model inference.

## Bounds and validation

Per run: **25 query areas**, **2 MiB/response**, **8 MiB retained responses**,
**2,000 features**, **50,000 returned/assembled vertices**, **1,000 displayed
matches** and **1 MiB finding display bytes**. Exclusions, truncations and omitted
findings are counted. The client follows no redirects and retries nothing
automatically; narrow selections or start a new run to recover.

Relation assembly, polygon-hole validation and geometric matching use the isolated
parser's CPU/memory/output/time bounds. It receives only response bytes,
coordinates and bounded settings, without credentials, case records or storage
paths. Snapshots/results use atomic publication and rollback cleanup.

Mocked synthetic tests cover current/historical queries, labels, roads, overlapping
polygons/holes, invalid/missing geometry, quotas, response/display limits, access
changes and exact retention. They establish no live availability, completeness
or positional accuracy.

Primary references: [Overpass historical queries](https://dev.overpass-api.de/overpass-doc/en/analysis/museum.html)
and [OSM geometry conversion](https://github.com/rapkin/osm2geojson).
