# Location analysis methods

Location analysis compares a selected set of up to 500 same-case observations.
Every result retains its selected source references and method parameters. The
original address, coordinates, event time, source row and checksum stay intact.
For counts, grouping and movement, select only one interpretation of each
original observation: selecting an original plus a geocoded/extracted version of
the same record is rejected. Location quality deliberately allows alternatives,
labels them as versions of one original and reports unique-observation counts.
The methods are shared by person and vehicle observations; a plain Location
record acquires no assumed person or vehicle association.

The deterministic methods use reported coordinates or an explicitly selected,
reviewed geocode. Address-only vehicle CSV rows need that spatial interpretation
before distance or movement analysis can use them. Derived coordinates never
replace reported coordinates. A comparison can retain both and flag disagreement.
No method verifies a person's presence, identifies a driver or infers an engine
state from zero reported speed.

## Available comparisons

| Method | Result and interpretation |
| --- | --- |
| Compare address wording | Retains raw text and conservative Unicode/whitespace derivatives, a leading street number when recognizable, and explicit unit identifiers. Limited street-suffix aliases can propose equivalent wording; units and unrecognized international formats remain distinguishable. Common street names do not establish geographic equivalence. |
| Review location quality | Separates event, receipt, collection and upload times. Shows source export timezone, absent event time/position, repeated-observation candidates, negative receipt delays, and event times reversed in retained export-row order. Selection order alone is not chronology. |
| Group nearby observations | Creates deterministic anchor-radius candidate groups. Each group exposes its anchor and original members; nearby groups do not establish visits, meetings or occupancy. There is no external road/building reference layer in this method. |
| Compare distance or geofence | Compares each usable coordinate with a selected reference observation, manual reference point, radius or polygon. Shows inside/outside candidate classifications and an explicit near-boundary ambiguity band. Missing coordinates remain excluded with a count. |
| Find candidate visits | Segments each retained subject association separately. Consecutive nearby samples must meet the minimum observed span; long gaps split candidates. Counts visits separately from samples, with first/last observed bounds and UTC visit-start patterns. Each excluded large gap is a separate finding linking its endpoint observations and actual elapsed interval. Frequent places are never automatically labelled home/work. |
| Find candidate journeys | Uses timed, associated observations, candidate stationary intervals and the maximum gap to delimit journeys. Links every included sample and exposes endpoints, largest sampling interval, elapsed time and straight-segment distance sum. Each excluded large gap exposes its last/next observations and actual elapsed interval. A straight segment is not a road route. |
| Compare movement patterns | Groups directed origin/destination pairs among the candidate journeys. Shows supporting samples, observed durations, UTC departure distributions and monthly counts with observation coverage. Explicit two-period comparisons expose denominators, pair shares, observed changes and interior-sample corridor candidates. Reversed pairs remain separate; missing periods and changing reporting density limit comparison. |
| Find candidate nearby subjects | Compares different retained person/vehicle associations within distance/time tolerances. Outputs the exact sample pair, distance and time difference. Point samples within a tolerance do not establish an overlapping presence interval, a meeting or a relationship. |
| Review travel discrepancies | Compares adjacent timed observations from the same retained subject association. Exposes same-time distant positions, unusually rapid transitions, source-row chronology problems and unsupported long gaps. Thresholds screen possible data/interpretation problems; no transport mode or deception is inferred. |
| Compare same-event location claims | Requires the investigator to describe the claimed event/place and explicitly select at least two observations. Presents their source claims, associations and time/distance differences side by side. Similar timestamps alone do not establish a contradiction. |

Coordinates have decimal precision but no retained measured accuracy. A reviewed
geocode may represent a venue, street or broad area. Candidate proximity and
boundary classifications therefore remain uncertain even when the arithmetic is
precise. Offline grouping uses coordinates alone. The separate
[automatic geographic-context operation](analysis-geographic-context.md) adds
retained map references with source, date, attribution and visible coverage limits.

## Parameters and bounds

| Parameter | Default | Supported range |
| --- | --- | --- |
| Nearby/stop distance | 100 m | 1–100,000 m |
| Coordinate discrepancy threshold (location quality and equal-time travel checks) | 100 m | 1–100,000 m |
| Maximum gap for movement segmentation | 60 min | 1–10,080 min |
| Minimum observed span for candidate visits | 5 min | 0–1,440 min; at least two samples still required |
| Co-location time tolerance | 10 min | 0–1,440 min |
| Rapid-transition screening threshold | 300 km/h | 1–3,000 km/h |
| Proximity query radius | 1,000 m | 1–500,000 m |
| Near-boundary ambiguity band | 10 m | 0–10,000 m |

Each form exposes only the assumptions its method uses. Location quality compares
reported and reviewed coordinates against the discrepancy threshold; travel checks
also use it for equal-time positions. Same-event comparison presents the selected
claims directly and has no distance or time tolerance setting.

Distances use a spherical Earth approximation, including observations near the
poles and dateline. Polygon inside/outside classification uses latitude/longitude
ray crossing, with a local projected boundary-distance approximation. Polygons
need 3–100 vertices. Split polygons crossing the dateline into separate queries;
self-intersecting and degenerate polygons are rejected. Very broad polygons
need investigator care and are not a surveying tool. Radius queries avoid this
polygon limitation.

Spatial groups are bounded by their first member anchor, rather than transitive
chains of nearby observations. Repeating the same selected originals and
parameters yields the same grouping. A candidate visit's observed first/last
samples do not establish continuous presence between samples or actual
arrival/departure times outside them. A gap can mean unavailable reporting rather
than inactivity. Co-location output is capped at 1,000 candidate pairs, with a
partial outcome and a request to narrow the selection when the cap is reached.

## Comparing movement across periods

Movement-pattern comparison accepts four timestamps together:
`period_a_start`, `period_a_end`, `period_b_start` and `period_b_end`. Each uses
an ISO timestamp with an explicit timezone, normalized to UTC; for example,
`2026-10-01T00:00:00+00:00`. A period includes its start and excludes its end.
Each end must follow its start, and the two periods must not overlap. Incomplete,
naive, reversed or overlapping windows are rejected. Journeys are segmented
independently within each period; a period boundary can truncate a candidate.

For each retained subject association, the result first exposes window duration,
selected timed observations, usable positions, positions missing from timed
observations, first/last usable observation, observed span, largest sampling
interval and excluded gap count. The sum of intervals under the maximum-gap
threshold is a sampling measure, not proof of continuous observation or presence.
Unknown event times cannot be assigned to a period. When a period has no samples,
its zero denominator and unknown interval measures remain visible, with links to
the overall selection context.

Directed endpoint pairs report their journey counts and their share of all
selected subject journeys in each period, UTC departure-hour distributions and
observed durations. A new or absent pair, or a changing share, is an observed
selection difference rather than suspicious behavior. Missing journey coverage
in either period yields an insufficient-comparison description instead of an
asserted behavior change. Without explicit periods, monthly counts retain full
calendar-month denominators and the selected observation coverage.

When both periods have journeys with at least one distinct observed interior
spatial group, the result compares ordered sample-group sequences. It exposes
shared and newly observed sequences and the group anchor coordinates, with
source links that include those reference anchors. A sparse journey without
interior samples cannot establish a corridor from its endpoints. Different
reporting density can change a sequence even along the same actual route;
these comparisons reconstruct no road-network path.

## Geocoding configuration and review

Geocoding supports an operator-configured Nominatim-compatible service. It has no
public default, and explicitly rejects `nominatim.openstreetmap.org` as a batch
processing endpoint. Operators must arrange an appropriate service/reference
dataset and follow its license and operating limits. The public service's
[usage policy](https://operations.osmfoundation.org/policies/nominatim/) has strict
traffic, application identification, caching and attribution requirements;
Pandect does not offer it as a generic geocoding backend.

Configuration is supplied to Django through these settings/environment mappings:

| Name | Meaning |
| --- | --- |
| `PANDECT_GEOCODER_URL` | Operator's Nominatim-compatible base endpoint. Empty disables lookups. |
| `PANDECT_GEOCODER_LOCAL` | Default `false`. Set `true` only for an operator-controlled endpoint; private-looking hostnames do not establish trust. |
| `PANDECT_GEOCODER_NAME` | Human-readable lookup source. |
| `PANDECT_GEOCODER_VERSION` | Operator-supplied reference dataset/version identifier. |
| `PANDECT_GEOCODER_ATTRIBUTION` | Required dataset attribution/license notice. |
| `PANDECT_GEOCODER_USER_AGENT` | Application/operator identification accepted by that service. |
| `PANDECT_GEOCODER_TIMEOUT` | Per-request timeout, default 10 seconds, bounded to 1–30 seconds. |

A lookup requires current edit permission and explicit external-processing
confirmation unless its endpoint is designated local. Authorization is rechecked
immediately before each request and before
publication by the processing service. The lookup sends only the selected
address or coordinate query, rather than case names, people, vehicle identifiers
or the evidence record. Already submitted requests cannot be recalled.

Each run is bounded to 25 distinct queries, with repeated identical queries
shared only within that same selected case/run. Requests accept at most five
candidate matches and 256 KiB of response data. Addresses longer than 2,000
characters are unsupported for lookup; the retained original may be longer.
Redirects are not followed. There are no client-side automatic retries.

Forward lookup recognizes imported comma-delimited Canadian addresses with at
least street, municipality, province, `CAN`, and a trailing Canadian-format
postcode. It submits the `CAN` component as Nominatim's `countrycodes=ca` filter
instead of free-text content, retaining the other address fields, including the
postcode. Other address formats are submitted unchanged. Each finding retains
the submitted query; adjusted lookups also retain the exact original address,
country filter and adjustment explanation, including no-match and failed lookups.
This makes one request per distinct original query, without broadening to a
street/city-only search. Returned addresses can still disagree with the source,
including its postcode; candidates remain estimates requiring review.

This query handling is geocoding method version 2. Upgrade web and worker together
and start a new run for previous no-match results. Queued version 1 work fails
recoverably with `processing_version_unavailable` on a version 2 worker; earlier
results and originals remain retained. No database migration is required.

[Forward lookup](https://nominatim.org/release-docs/latest/api/Search/) retains
candidate coordinates, display address, match granularity, bounding box, source
reference, attribution, lookup time, query and reference version. Alternatives
remain distinct. [Reverse lookup](https://nominatim.org/release-docs/latest/api/Reverse/)
returns a nearby indexed map object and can disagree with the address of the
actual point. Neither direction establishes measured accuracy.

A geocode begins unresolved. Investigators review a specific candidate before it
can be selected as a spatial input; accepting an entire run must not silently
choose among alternatives. Rejections, unresolved outcomes, explanations and
later reruns retain their history. No-match, unsupported, quota/authentication,
invalid response and unavailable-service outcomes remain distinguishable. A
partly failed batch retains completed candidates with a partial outcome instead
of claiming every lookup succeeded. A selection with no eligible addresses or
coordinates is reported as insufficient and does not contact the lookup service.

The [investigation map](maps.md#local-address-lookup) can queue local forward
lookups and display unreviewed candidates as distinct square estimate markers.
This is candidate inspection, not acceptance for location analysis. Every
unrejected alternative remains visible, accepted candidates are labelled, and
the original observation is preserved. The map never invokes an external geocoder.

These choices follow the distinction between observed samples and inferred
trajectory interpretations discussed in Microsoft's
[trajectory-data mining research](https://www.microsoft.com/en-us/research/project/trajectory-data-mining/).
Road-network reconstruction and inferred transport modes require additional
inputs and evaluation beyond these methods.

## Automatic vehicle trips

Vehicle pages and the **Vehicle trips** report calculate possible trips from the
current vehicle-location evidence as soon as observations are available. This
includes address-only Asset Location CSV imports after their associations and time
interpretation have been confirmed. No analysis job, geocoding lookup, or finding
review is needed. The existing coordinate-based retained journey operation remains
available for a deliberately selected, versioned analysis.

The current-evidence report uses at most the latest 500 matching observations by
reported observation time and shows its coverage. Vehicle and UTC date filters let
investigators inspect another period; archived observations are excluded by default.
Vehicle pages show the five most recent possible trips and link to the full report.
Untimed observations cannot form trips. Each vehicle is segmented independently,
using only evidence whose vehicle and import provenance remain in the same case.

Reported coordinates take precedence when both observations have them; positions
within 100 metres are treated as nearby. Otherwise conservative matching address
wording groups nearby observations without inferring coordinates. A stationary
group lasting at least five observed minutes, with no positive reported speed,
separates possible trips. Gaps longer than one hour, conflicting positions at the
same time, and positions that cannot be compared also split the sequence. Stationary
observations alone do not produce a trip. Address wording is an approximation and
does not establish a physical stop.

Trips show their first and last observed place/time, elapsed observed minutes, all
contributing observation links, and imported CSV row links. Actual departure,
arrival, road route, distance travelled, driver, and continuous presence remain
unknown. Coverage gaps are available alongside trips. Results are recalculated on
the next page load when observations are added or archived; these reports do not
create retained analysis results or change original evidence.
