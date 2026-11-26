"""Compare reported locations, address wording, and spatial claims."""

import math
import re
import unicodedata
from collections import defaultdict
from itertools import combinations

from core.analysis.sources import output
from geo.analysis.deterministic.observations import (
    cluster_observations,
    format_observed_time,
    location_item,
    number,
    observation_metrics,
    observation_point,
    observation_warnings,
    observed_time,
    position_kind,
    selected_observations,
    source_order_discrepancies,
    subject_key,
)
from geo.coordinates import (
    EARTH_RADIUS_M,
    coordinate,
    distance_m,
)


def normalize_address(address):
    "Conservative derivatives; unsupported formats remain text-only matches."
    raw = str(address or "")
    normalized = " ".join(unicodedata.normalize("NFKC", raw).casefold().split())
    normalized = re.sub(r"\s*,\s*", ", ", normalized)
    unit = re.search(r"(?:\b(?:unit|apt|apartment|suite)\s*|#\s*)([\w-]+)", normalized)
    number_match = re.match(r"(\d+[a-z]?(?:-\d+)?)\s+(.+)", normalized)
    components = {
        "normalized": normalized,
        "unit": unit.group(1) if unit else "",
        "street_number": number_match.group(1) if number_match else "",
        "supported_pattern": bool(number_match),
    }
    # Expand only unambiguous street suffix tokens, not names such as St John.
    aliases = {"rd": "road", "st": "street", "ave": "avenue", "blvd": "boulevard"}
    canonical = re.sub(
        r"\b(rd|st|ave|blvd)\.(?=\s*(?:,|$))|\b(rd|st|ave|blvd)(?=\s*(?:,|$))",
        lambda m: aliases[m.group(1) or m.group(2)],
        normalized,
    )
    components["comparison_key"] = canonical
    return components


def normalize_addresses(sources, parameters):
    sources = selected_observations(sources)
    groups = defaultdict(list)
    items = []
    for source in sources:
        raw = getattr(source.item, "address", "")
        parsed = normalize_address(raw)
        items.append(
            location_item(
                "Address derivative",
                "Original address is retained; normalization is a comparison aid.",
                [source],
                Original=raw,
                Normalized=parsed["normalized"],
                Unit=parsed["unit"] or "Not identified",
                **{
                    "Street number": parsed["street_number"] or "Not identified",
                    "Parsing scope": "Leading street number pattern"
                    if parsed["supported_pattern"]
                    else "Unmatched address pattern",
                    "Normalization version": "conservative-address.v1",
                },
            )
        )
        if parsed["comparison_key"]:
            groups[parsed["comparison_key"]].append(source)
    matches = [group for group in groups.values() if len(group) > 1]
    for group in matches:
        items.append(
            location_item(
                "Candidate equivalent wording",
                (
                    "Same conservatively normalized address, including any "
                    "explicit unit. Review geography, historical names and "
                    "independent source context before accepting equivalence."
                ),
                group,
                **{
                    "Matching observations": len(group),
                    "Status": "Unresolved candidate",
                },
            )
        )
    return output(
        "Compare original addresses and conservative derivatives.",
        items=items,
        metrics=observation_metrics(sources)
        | {"Candidate wording groups": len(matches)},
        warnings=[
            (
                "Identical wording and a common street name do not establish "
                "the same place. Units are preserved; unsupported "
                "international formats remain unmatched."
            )
        ],
        outcome="complete" if sources else "empty",
    )


def location_quality(sources, parameters):
    sources = selected_observations(sources, allow_alternatives=True)
    radius = number(parameters, "distance_m")
    items = []
    signatures = defaultdict(list)
    for source in sources:
        item = source.item
        position, observed = observation_point(source), observed_time(source)
        flags = []
        if observed is None:
            flags.append("Event time is missing or lacks an explicit timezone")
        if position is None:
            flags.append("No usable coordinates; spatial checks unavailable")
        if (
            source.derived_coordinates is not None
            and getattr(item, "latitude", None) is not None
        ):
            discrepancy = distance_m(position, coordinate(*source.derived_coordinates))
            if discrepancy > radius:
                flags.append(
                    "Reported coordinates and reviewed address interpretation differ "
                    f"by {discrepancy:.1f} m"
                )
        receipt = getattr(item, "received_at", None)
        if observed and receipt and receipt < observed:
            flags.append(
                "Receipt precedes event time; field semantics or clocks need review"
            )
        timezone = getattr(getattr(item, "import_batch", None), "timezone", "")
        items.append(
            location_item(
                "Observation quality",
                "; ".join(flags)
                or "No configured discrepancy found; source accuracy remains unknown.",
                [source],
                **{
                    "Event time": format_observed_time(observed),
                    "Receipt time": format_observed_time(receipt),
                    "Collection time": format_observed_time(
                        getattr(item, "collected_at", None)
                    ),
                    "Upload time": format_observed_time(
                        getattr(item, "created_at", None)
                    ),
                    "Export timezone": timezone or "Not recorded",
                    "Position origin": position_kind(source),
                    "Address": getattr(item, "address", "") or "Not recorded",
                    "Reviewed derivative coordinates": str(source.derived_coordinates)
                    if source.derived_coordinates is not None
                    else "Not selected",
                },
            )
        )
        signatures[
            (
                subject_key(source),
                position,
                format_observed_time(observed),
                getattr(item, "address", ""),
            )
        ].append(source)
    interpretations = defaultdict(list)
    for source in sources:
        interpretations[(source.kind, str(source.item.pk))].append(source)
    for group in interpretations.values():
        if len(group) > 1:
            items.append(
                location_item(
                    "Alternative interpretations of one observation",
                    "These selected versions refer to one retained original. "
                    "They are alternatives, not independent samples or corroboration.",
                    group,
                    **{"Selected interpretations": len(group)},
                )
            )
    for group in signatures.values():
        originals = {(source.kind, str(source.item.pk)) for source in group}
        if len(originals) > 1:
            items.append(
                location_item(
                    "Repeated-observation candidate",
                    "Identical selected location/time/association fields; "
                    "independent original occurrences remain separate.",
                    group,
                    **{"Original occurrences": len(originals)},
                )
            )
    items.extend(source_order_discrepancies(sources))
    return output(
        ("Review time semantics, positional interpretation and repeated observations."),
        items=items,
        metrics=observation_metrics(sources)
        | {
            "Unique original observations": len(interpretations),
            "Selected interpretations": len(sources),
        },
        warnings=observation_warnings(sources)
        + [
            "Alternative versions of one original are compared deliberately here. "
            "They must not be counted as independent observations or visits."
        ],
        outcome="complete" if sources else "empty",
    )


def spatial_groups(sources, parameters):
    sources = selected_observations(sources)
    radius = number(parameters, "distance_m")
    groups = cluster_observations(sources, radius)
    items = [
        location_item(
            f"Spatial group {index + 1}",
            (
                "Candidate grouping within the configured radius of its first"
                " source anchor; not a visit or occupancy claim."
            ),
            group,
            **{
                "Observations": len(group),
                "Anchor latitude": observation_point(group[0])[0],
                "Anchor longitude": observation_point(group[0])[1],
                "Anchor radius (m)": radius,
                "Position origins": ", ".join(
                    sorted({position_kind(s) for s in group})
                ),
            },
        )
        for index, group in enumerate(groups)
    ]
    return output(
        "Inspect groups of nearby observed or reviewed positions.",
        items=items,
        metrics=observation_metrics(sources) | {"Spatial groups": len(groups)},
        warnings=observation_warnings(sources)
        + [
            (
                "No external map layer is used. Coordinates supply geographic"
                " context; no road, venue or building identity is inferred."
            )
        ],
        outcome="complete" if groups else "insufficient",
    )


def _edge_distance(point, first, second):
    # Local equirectangular projection is used only to flag boundary ambiguity.
    scale = math.cos(math.radians(point[0]))
    x, y = math.radians(point[1]) * scale, math.radians(point[0])
    ax, ay = math.radians(first[1]) * scale, math.radians(first[0])
    bx, by = math.radians(second[1]) * scale, math.radians(second[0])
    length = (bx - ax) ** 2 + (by - ay) ** 2
    ratio = (
        max(0, min(1, ((x - ax) * (bx - ax) + (y - ay) * (by - ay)) / length))
        if length
        else 0
    )
    return EARTH_RADIUS_M * math.hypot(
        x - ax - ratio * (bx - ax), y - ay - ratio * (by - ay)
    )


def _polygon(vertices):
    if not isinstance(vertices, list) or not 3 <= len(vertices) <= 100:
        raise ValueError("A geofence needs 3 to 100 latitude/longitude vertices.")
    points = [coordinate(*point) for point in vertices]
    if points[0] == points[-1]:
        points.pop()
    if len(points) < 3 or len(set(points)) != len(points):
        raise ValueError("A geofence needs at least three distinct vertices.")
    if max(p[1] for p in points) - min(p[1] for p in points) > 180:
        raise ValueError(
            "Split a geofence crossing the dateline into separate queries."
        )
    edges = list(zip(points, points[1:] + points[:1], strict=True))
    area = sum(a[0] * b[1] - b[0] * a[1] for a, b in edges)
    if abs(area) < 1e-12:
        raise ValueError("A geofence must enclose a nonzero area.")

    def side(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    for index, (a, b) in enumerate(edges):
        for second_index in range(index + 1, len(edges)):
            if second_index == index + 1 or (
                index == 0 and second_index == len(edges) - 1
            ):
                continue
            c, d = edges[second_index]
            if (
                side(a, b, c) * side(a, b, d) <= 0
                and side(c, d, a) * side(c, d, b) <= 0
            ):
                if max(min(a[0], b[0]), min(c[0], d[0])) <= min(
                    max(a[0], b[0]), max(c[0], d[0])
                ) and max(min(a[1], b[1]), min(c[1], d[1])) <= min(
                    max(a[1], b[1]), max(c[1], d[1])
                ):
                    raise ValueError("A geofence must not intersect itself.")
    return points


def proximity(sources, parameters):
    sources = selected_observations(sources)
    boundary = number(parameters, "boundary_m")
    polygon = parameters.get("polygon")
    if polygon is not None:
        polygon = _polygon(polygon)
        reference, radius = None, None
    else:
        reference_id = str(parameters.get("reference_source_id", ""))
        if reference_id:
            reference_source = next(
                (s for s in sources if str(s.id) == reference_id), None
            )
            if reference_source is None or observation_point(reference_source) is None:
                raise ValueError(
                    "The reference must be a selected coordinate-bearing observation."
                )
            reference = observation_point(reference_source)
        else:
            reference = coordinate(
                parameters.get("reference_latitude"),
                parameters.get("reference_longitude"),
            )
        radius = number(parameters, "radius_m")
    items = []
    for source in sources:
        point = observation_point(source)
        if point is None:
            continue
        if polygon:
            inside = False
            edges = list(zip(polygon, polygon[1:] + polygon[:1], strict=True))
            for first, second in edges:
                if (first[0] > point[0]) != (second[0] > point[0]):
                    crossing = (second[1] - first[1]) * (point[0] - first[0]) / (
                        second[0] - first[0]
                    ) + first[1]
                    if point[1] < crossing:
                        inside = not inside
            boundary_distance = min(
                _edge_distance(point, first, second) for first, second in edges
            )
            classification = (
                "Near boundary (uncertain)"
                if boundary_distance <= boundary
                else "Inside candidate area"
                if inside
                else "Outside candidate area"
            )
            values = {"Boundary distance estimate (m)": round(boundary_distance, 2)}
        else:
            distance = distance_m(reference, point)
            classification = (
                "Near boundary (uncertain)"
                if abs(distance - radius) <= boundary
                else "Inside candidate radius"
                if distance < radius
                else "Outside candidate radius"
            )
            values = {"Distance estimate (m)": round(distance, 2), "Radius (m)": radius}
        items.append(
            location_item(
                classification,
                (
                    "Geometric comparison; unknown accuracy can change the "
                    "classification and does not verify presence."
                ),
                [source],
                **values,
                **{"Position origin": position_kind(source)},
            )
        )
    return output(
        "Compare selected observations with the configured radius or geofence.",
        items=items,
        metrics=observation_metrics(sources) | {"Compared observations": len(items)},
        warnings=observation_warnings(sources)
        + [
            f"Boundary ambiguity band: {boundary:g} m. Spherical distance estimates; "
            "polygon boundary distances use a local projection."
        ],
        outcome="complete" if items else "insufficient",
    )


def colocation(sources, parameters):
    sources = selected_observations(sources)
    radius = number(parameters, "distance_m")
    window = number(parameters, "time_window_minutes") * 60
    observed = [
        source
        for source in sources
        if subject_key(source) is not None
        and observed_time(source)
        and observation_point(source) is not None
    ]
    items = []
    for first, second in combinations(observed, 2):
        if subject_key(first) == subject_key(second):
            continue
        seconds = abs((observed_time(first) - observed_time(second)).total_seconds())
        if seconds > window:
            continue
        distance = distance_m(observation_point(first), observation_point(second))
        if distance <= radius:
            items.append(
                location_item(
                    "Candidate nearby observations",
                    (
                        "Two associated samples fit the selected "
                        "distance/time tolerances. This does not establish a "
                        "meeting, relationship, coordination or shared "
                        "activity; a large venue may contain unrelated "
                        "subjects."
                    ),
                    [first, second],
                    **{
                        "Distance estimate (m)": round(distance, 2),
                        "Time difference (seconds)": seconds,
                        "First event time": format_observed_time(observed_time(first)),
                        "Second event time": format_observed_time(
                            observed_time(second)
                        ),
                        "Position origins": (
                            f"{position_kind(first)}; {position_kind(second)}"
                        ),
                        "Temporal interpretation": (
                            "Point samples within tolerance; "
                            "no observed presence overlap interval"
                        ),
                    },
                )
            )
            if len(items) >= 1000:
                break
    warnings = observation_warnings(sources, movement=True)
    if len(items) >= 1000:
        warnings.append(
            "Candidate output is capped at 1,000 pairs; narrow the "
            "selection to inspect all possible pairs."
        )
    return output(
        "Inspect candidate co-location with source/time/position limitations.",
        items=items,
        metrics=observation_metrics(sources)
        | {
            "Candidate pairs": len(items),
            "Time tolerance (minutes)": window / 60,
            "Distance tolerance (m)": radius,
        },
        warnings=warnings,
        outcome="partial"
        if len(items) >= 1000
        else "complete"
        if observed
        else "insufficient",
    )


def location_conflicts(sources, parameters):
    sources = selected_observations(sources)
    event = parameters.get("event_label", "")
    if not isinstance(event, str) or not event.strip() or len(event) > 200:
        raise ValueError(
            "Describe the same claimed event or place being explicitly "
            "compared (up to 200 characters)."
        )
    if len(sources) < 2:
        return output(
            "Select at least two observations for the explicitly claimed event.",
            outcome="insufficient",
        )
    first = sources[0]
    items = []
    for source in sources:
        values = {
            "Claimed event/place": event,
            "Address": getattr(source.item, "address", "") or "Not recorded",
            "Event time": format_observed_time(observed_time(source)),
            "Subject association": subject_key(source) or "Unlinked observation",
            "Position origin": position_kind(source),
            "Source citation": getattr(source.item, "source", "") or "Not recorded",
        }
        if observation_point(source):
            values |= {
                "Latitude": observation_point(source)[0],
                "Longitude": observation_point(source)[1],
            }
        if observation_point(source) and observation_point(first):
            values["Distance from first selected observation (m)"] = round(
                distance_m(observation_point(source), observation_point(first)), 2
            )
        if observed_time(source) and observed_time(first):
            values["Time difference from first selected observation (seconds)"] = (
                observed_time(source) - observed_time(first)
            ).total_seconds()
        items.append(
            location_item(
                "Selected observation comparison",
                (
                    "This comparison reflects the investigator's explicit "
                    "same-event/place selection. Differences remain "
                    "unresolved; similar timestamps alone do not establish a "
                    "contradiction."
                ),
                [source],
                **values,
            )
        )
    return output(
        (
            "Compare independent source claims side by side without selecting"
            " a true observation."
        ),
        items=items,
        metrics=observation_metrics(sources),
        warnings=observation_warnings(sources)
        + [
            (
                "Accept, reject or leave the interpretation unresolved "
                "through retained review; original observations remain "
                "unchanged."
            )
        ],
    )
