# Location analysis methods

Retained runs compare up to 500 same-case observations, preserving selection,
parameters and original addresses, coordinates, times, rows and checksums.
Counts/grouping/movement require one interpretation per original; selecting both
original and geocoded/extracted versions is rejected. Quality review allows
labelled alternatives and counts unique observations. Person/vehicle methods
also accept plain Locations without assigning subjects.

Deterministic methods use reported coordinates or selected address estimates.
Address-only CSV rows need interpretation for spatial analysis; [automatic
trips](#automatic-vehicle-trips) also work from addresses. Derived coordinates
never replace reported ones; comparisons can retain both and flag disagreement.
No method verifies presence, identifies drivers or infers engine state from zero speed.

## Available comparisons

| Method | Result and interpretation |
| --- | --- |
| Compare address wording | Raw text, conservative Unicode/whitespace derivatives, recognizable leading street numbers and explicit units. Limited street-suffix aliases suggest equivalent wording; units and unrecognized international formats remain distinct. Common street names prove no geographic equivalence. |
| Review location quality | Event/receipt/collection/upload times, export timezone, missing times/positions, possible repeats, negative receipt delays and reversed event times in export-row order. Selection order is not chronology. |
| Group nearby observations | Deterministic anchor-radius groups with original members. Proximity proves no visits, meetings or occupancy; no road/building reference layer is used. |
| Compare distance or geofence | Compares coordinates to an investigator-supplied centre/radius or polygon, showing inside/outside candidates and boundary ambiguity. Missing coordinates are counted exclusions. |
| Find candidate visits | Segments subjects separately. Consecutive nearby samples need the minimum observed span; large gaps split candidates. Retains visit/sample counts, first/last bounds, UTC start patterns and each excluded gap's endpoints/elapsed time. Frequent places receive no home/work labels. |
| Find candidate journeys | Uses timed subject observations, stationary candidates and maximum gaps. Retains every included sample, endpoints, largest sampling interval, elapsed time, straight-segment distance sum and each excluded gap's endpoints/interval. Segments are not road routes. |
| Compare movement patterns | Directed journey endpoint pairs, samples, durations, UTC departures and monthly counts/coverage. Two-period comparisons expose denominators, pair shares, differences and interior-sample corridor candidates. Reversed pairs stay separate; missing periods/reporting density limit comparison. |
| Find candidate nearby subjects | Exact pairs of different person/vehicle associations within time/distance tolerances. Point samples establish no overlapping presence interval, meeting or relationship. |
| Review travel discrepancies | Adjacent same-subject times/positions: equal-time distant points, rapid transitions, row-order problems and unsupported gaps. Thresholds screen interpretation/data issues, without inferring transport mode or deception. |
| Compare same-event location claims | At least two selected observations plus an investigator-described event/place. Compares claims, associations and time/distance differences; similar timestamps alone establish no contradiction. |

Coordinates retain decimal precision, not measured accuracy; address estimates
may describe broad areas. Precise arithmetic does not remove proximity/boundary
uncertainty. Offline grouping uses coordinates alone; separate
[geographic lookup](analysis-geographic-context.md) retains attributed, dated
references and coverage limits.

## Parameters and bounds

| Parameter | Investigator form default | Supported range |
| --- | --- | --- |
| Nearby/stop distance | 100 m | 1–100,000 m |
| Coordinate discrepancy threshold (location quality and equal-time travel checks) | 100 m | 1–100,000 m |
| Maximum gap for movement segmentation | 60 min | 1–10,080 min |
| Minimum observed span for candidate visits | 5 min | 0–1,440 min; at least two samples still required |
| Co-location time tolerance | 10 min | 0–1,440 min |
| Rapid-transition screening threshold | 300 km/h | 1–3,000 km/h |
| Proximity query radius | 100 m | 1–500,000 m |
| Near-boundary ambiguity band | 10 m | 0–10,000 m |

Forms expose only relevant assumptions. Quality review compares reported and estimated
coordinates against the discrepancy threshold; travel checks use it for equal-time
positions. Same-event comparison has no time/distance tolerance.

Omitted or cleared `radius_m` uses the processor's 1,000 m default. Internal
`reference_source_id` can designate a selected coordinate-bearing centre, but
forms offer only manual coordinates or polygons.

Distances use a spherical Earth approximation, including poles/dateline. Polygon
classification uses latitude/longitude ray crossing and locally projected boundary
distances. Polygons require 3–100 vertices; self-intersections/degeneracy are
rejected. Split dateline-crossing polygons into queries. Broad polygons need care;
this is no surveying tool. Radius queries avoid the polygon limitation.

Groups use first-member anchors, without transitive proximity chains. Identical
originals/parameters give identical groups. Visit first/last samples establish
neither continuous presence nor actual arrival/departure. Gaps can reflect missing
reporting. Co-location stops at 1,000 pairs with partial coverage and narrowing
instructions.

## Comparing movement across periods

Fill all four movement-period fields in UTC. The form uses UTC independently of
your account or device timezone. Saved parameters use ISO timestamps:
`period_a_start`, `period_a_end`, `period_b_start`, `period_b_end`, e.g.
`2026-10-01T00:00:00+00:00`. Times normalize to UTC; windows include starts and
exclude ends. Ends must follow starts, without overlap. Incomplete, naive, reversed
or overlapping windows are rejected. Separate segmentation can truncate journeys
at period boundaries.

Per subject/window, results retain duration, timed observations, usable/missing
positions, first/last usable observations, observed span, largest sampling interval
and excluded-gap count. Summed intervals below the gap threshold measure sampling,
not continuous observation/presence. Unknown times remain unassigned. Empty periods
show zero denominators, unknown interval measures and overall selection links.

Directed pairs retain journey counts/shares, UTC departure distributions and
observed durations. New/absent pairs or changed shares describe selections, not
suspicious behavior. Missing journey coverage produces insufficient comparison,
not asserted behavior change. Without periods, monthly counts retain full-month
denominators and selected coverage.

When each period has journeys containing a distinct interior spatial group,
ordered group sequences expose shared/new sequences, anchors and source links.
Endpoints alone cannot establish corridors; reporting density can change sequences
along the same actual route. No road-network path is reconstructed.

## Geocoding configuration and estimate selection

Use an operator-configured Nominatim-compatible service. No public default exists;
`nominatim.openstreetmap.org` is rejected for batch processing. Operators arrange
capacity/datasets/licensing. The public [usage policy](https://operations.osmfoundation.org/policies/nominatim/)
restricts traffic and requires identification, caching and attribution; Pandect
does not offer that service as a generic backend.

| Name | Meaning |
| --- | --- |
| `PANDECT_GEOCODER_URL` | Nominatim-compatible base endpoint; empty disables lookup. |
| `PANDECT_GEOCODER_LOCAL` | Default `false`; set `true` only for operator control, never inferred from a private hostname. |
| `PANDECT_GEOCODER_NAME` | Human-readable source. |
| `PANDECT_GEOCODER_VERSION` | Dataset/version identifier. |
| `PANDECT_GEOCODER_ATTRIBUTION` | Required attribution/license notice. |
| `PANDECT_GEOCODER_USER_AGENT` | Application/operator identification accepted by the service. |

Requests have a fixed ten-second timeout. Service identification defaults to
`Pandect/0.1`; override it when the operator requires different contact details.

Processing requires current edit access. A service designated external also
requires case-owner approval in Case Settings. Processing rechecks access and
approval immediately before requests and publication.
Only selected addresses/coordinate queries are sent, without case/person/vehicle
identifiers or evidence records. Sent requests cannot be recalled.

Runs allow 25 distinct queries, sharing identical queries only within that case/run.
Responses allow five matches and 256 KiB. Lookup addresses above 2,000 characters
are unsupported; originals can be longer. No redirects or automatic retries occur.

For imported comma-delimited Canadian addresses containing street, municipality,
province, `CAN` and trailing Canadian-format postcode, forward lookup sends
`CAN` as `countrycodes=ca`, preserving other fields/postcode. Other formats pass
unchanged. Findings retain submitted queries; adjustments retain original address,
country filter and explanation even on failure/no match. Each distinct original
gets one request, without street/city-only broadening. Returned addresses can
still disagree, including postcodes. Choose an estimate before using it.

This is geocoding version 2. Upgrade web/workers together and rerun prior no-matches.
Queued version 1 fails recoverably with `processing_version_unavailable`; originals
and prior results remain unchanged. No database migration is needed.

[Forward lookup](https://nominatim.org/release-docs/latest/api/Search/) retains
coordinates, display address, granularity, bounding box, source reference,
attribution, lookup time, query and version, keeping alternatives distinct.
[Reverse lookup](https://nominatim.org/release-docs/latest/api/Reverse/) returns
a nearby indexed object that may disagree with the actual point's address.
Neither establishes measured accuracy.

For spatial use, open a candidate and choose **Use this estimate**. Choose **Stop
using this estimate** to clear it. Saved runs retain the original estimate they
used. No-match, unsupported, quota/authentication,
invalid-response and unavailable-service outcomes stay distinct. Partly failed
batches retain candidates with partial coverage. No eligible address/coordinate
means insufficient evidence and no service request.

The [Map](maps.md#local-address-lookup) prepares configured-local lookup for saved
trip sources and links to each estimate. Only one currently selected candidate
can supply an estimated trip position. Missing or ambiguous points leave breaks.
Originals stay unchanged. The map never invokes external geocoding.

## Automatic vehicle trips

Map, Vehicle trips and vehicle pages share saved possible trips. New observations,
completed CSV imports and archive/restore queue vehicle rebuilding. Existing cases
use idempotent **Prepare trips** POSTs, automatically started by owners/editors
opening the JavaScript map or through the normal form. Viewers inspect results;
Processing shows progress, generations, safe failures and recovery.

Background chunks read 500 observations from complete chronological histories,
without a total-source cutoff. Generations retain every source/checksum and
active/archived/all variants. Each chunk rechecks access/revision; only complete
matching work publishes. Source changes supersede work; retries/duplicate delivery
cannot publish partial trips. Confirm stopped workers before recovery, with at
most three attempts per recovery phase. Startup/migrations never process evidence.

Vehicle pages show five recent trips; map/report pages show 25. Date filters retain
complete overlapping trips, without shortening input sequences. Times use account/
device timezone, falling back to UTC. Archived observations are excluded by default.
Trip detail leads with the first and last recorded observations and the elapsed
span. The source list exposes every observation and imported row provenance.
An evidence reader links back when its exact observation belongs to one
current possible trip in its active or archived selection.

Vehicles segment independently using same-case observations and consistent import/
document relationships. Untimed rows remain in coverage but cannot form trips.
Coordinates take precedence when both exist; positions within 100 metres are
nearby. Otherwise conservative address normalization compares places without
inferring coordinates. Stationary groups split trips after five observed minutes
with no positive speed anywhere. Gaps above one hour, conflicting equal-time
positions and incomparable positions also split sequences. Stationary-only
observations form no trip.

Address-only CSV trips can be built from original addresses. After publication, configured private
lookup separately prepares candidates. Maps connect reported positions or exactly
one selected local estimate, retaining attribution and unresolved breaks. Selecting
an estimate changes the mapped path. See [map lookup](maps.md#local-address-lookup).

Trips retain first/last observed places/times and elapsed minutes. Actual departure,
arrival, road route, travelled distance, driver and continuous presence remain
unknown. Deliberately selected retained journey analysis remains separately
available within its source limits.
