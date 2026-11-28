# Location analysis methods

Retained runs compare up to 500 same-case observations and preserve original
values, row provenance, checksums and parameters. Counts, grouping and movement
use one interpretation per original. Quality review can show alternatives and
counts each original once. Plain Locations remain unassigned.

Spatial methods use reported coordinates or selected estimates. Address-only
rows need an estimate for coordinate comparisons. [Automatic trips](#automatic-vehicle-trips)
can also use addresses. Derived coordinates stay separate from reported
coordinates, and comparisons can flag disagreements.

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

## Compare plans with locations

Open **Case Tools → Analysis methods → Compare plans with locations**. Select
at least one quoted passage from a message, captured text, supplied transcript
or exact retained extraction, OCR or transcription. A saved claim suggestion
can supply the quote with its AI attribution. Select at least one observation,
or include the samples from a retained visit, journey or active vehicle trip.
The picker shows saved trip observation counts. If a saved trip, journey or
visit would use all available source slots before adding the required statement,
the picker links back to individual observations. This method runs locally.

Select the exact occurrence, subject, place and any interpretable time with UTC
offset. Cite the reference for a relative time. Use one interpretation for
people or vehicles named together. Add separate interpretations for ambiguous
subjects, places or times. The reported sender and recipient stay separate from
the claim subjects.

The result cites supporting and contrary samples for each subject and discloses
gaps and unresolved readings. A conflicting sample can raise a discrepancy
for a claimed instant. A matching record supports the comparison while leaving
attendance, driver identity and continuity unresolved.

**Restart source selection** restores available selections, searches and pages.
**Cancel** and result return links reopen the starting record and filters while
the record is available. Open a passage for highlighted wording and page, line
or recording anchors. **Browse related exchanges** opens existing cross-channel
findings for a cited message or call.

The run keeps the trip generation and ordered members. Changed inputs or revoked
access withhold dependent material. Each comparison allows up to 500 source
snapshots, 1,000 interpretation–observation pairs, 32 KiB settings, 4 MiB results
and 16 MiB loaded material by default. Narrow oversized selections.

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

Clearing the radius uses the processor's 1,000 m default.

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

Trips and vehicle details share saved possible trips. New observations,
completed CSV imports and archive/restore queue vehicle rebuilding. Existing cases
use idempotent **Prepare trips** POSTs. Owners and editors can trigger preparation
by opening Trips with JavaScript enabled or by submitting the form. Viewers can
inspect results. Processing shows progress, generations, failures, and recovery.

Workers read up to 500 active observations per chunk across the full vehicle
history. Each chunk verifies access and the source revision. Only a complete,
current generation publishes. Changes can supersede an active rebuild. The
current generation and any generation cited by a saved or queued analysis or
Evidence chat retain their input snapshots. Cleanup retires uncited derived rows
in bounded steps and keeps generation history. Confirm the previous worker
stopped before recovery. Each recovery phase allows up to three attempts.

Vehicle pages show up to five recent trips. Trips shows 25 trips per page and
jumps to the first whole trip overlapping a selected day. Times use the account
or device timezone, falling back to UTC. Trip processing uses active observations
across the complete vehicle history. See [Trips and the investigation map](maps.md).
Trip detail leads with the first and last recorded observations and the elapsed
span. The source list exposes every observation and imported row provenance.
An evidence reader links back when its exact active observation belongs to one
current possible trip.

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

First and last recorded places and times bound the observed span. Actual
departure, arrival, route, travelled distance, driver and continuous presence
remain unknown.

### Saved trips in Evidence chat

For a vehicle name, plate or VIN, chat can read up to three recent published
possible trips. It can also open the trip containing a shortlisted observation
or compare a timed message with trips recorded within one hour of its timestamp.
Date-only messages supply no time of day. A comparison can follow the first and
last observations to nearby timed messages. The one-hour search window identifies
candidates for reading. Timing alone does not establish a relationship.

Each chat trip reference links to **Saved possible trip** with the times, places
and supporting observations from that specific generation. Chat can verify up
to 64 observations per trip; the Trips workspace provides longer histories.
A rebuild preserves cited generations. Archiving a contributing observation or
its source export withholds the dependent reply. Confirmed permanent deletion
includes replies and later conversation turns that depend on the retired
generation.
