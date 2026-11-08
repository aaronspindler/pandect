"""Bounded, deterministic comparisons of retained location observations.

These methods describe observations and candidate interpretations. They never
assign an unlinked observation to a subject or reconstruct a road route.
"""

import math
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import UTC, datetime
from itertools import combinations, pairwise

from core.analysis.sources import output
from core.evidence.types import LOCATION_KINDS

VERSION = "location-observations.v2"
MAX_SOURCES = 500
EARTH_RADIUS_M = 6_371_008.8
LIMITS = {
    "distance_m": (100, 1, 100_000),
    "gap_minutes": (60, 1, 10_080),
    "dwell_minutes": (5, 0, 1_440),
    "time_window_minutes": (10, 0, 1_440),
    "speed_limit_kmh": (300, 1, 3_000),
    "radius_m": (1_000, 1, 500_000),
    "boundary_m": (10, 0, 10_000),
}
CAUTION = (
    "Positions have no retained measured accuracy. Decimal precision is not accuracy; "
    "reviewed geocodes may be building, street or area estimates."
)
MOVEMENT_CAUTION = (
    "Candidates describe selected observations, not continuous presence, a verified "
    "route, transport mode, driver identity, or proof of a person's presence."
)


def number(parameters, key):
    default, minimum, maximum = LIMITS[key]
    raw = parameters.get(key, default)
    if isinstance(raw, bool):
        raise ValueError(f"{key} must be a finite number.")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a finite number.") from exc
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}.")
    return value


def coordinate(latitude, longitude):
    if isinstance(latitude, bool) or isinstance(longitude, bool):
        raise ValueError("Coordinates must be finite decimal degrees.")
    try:
        point = float(latitude), float(longitude)
    except (TypeError, ValueError) as exc:
        raise ValueError("Coordinates must be finite decimal degrees.") from exc
    if not all(math.isfinite(x) for x in point):
        raise ValueError("Coordinates must be finite decimal degrees.")
    if not -90 <= point[0] <= 90 or not -180 <= point[1] <= 180:
        raise ValueError("Coordinates are outside WGS84 latitude/longitude bounds.")
    return point


def distance_m(first, second):
    "Spherical surface distance, including dateline and polar observations."
    lat1, lon1 = map(math.radians, coordinate(*first))
    lat2, lon2 = map(math.radians, coordinate(*second))
    term = math.sin((lat2 - lat1) / 2) ** 2
    term += math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1, math.sqrt(max(0, term))))


def _selected(sources, *, allow_alternatives=False):
    sources = list(sources)
    if len(sources) > MAX_SOURCES:
        raise ValueError(f"Select at most {MAX_SOURCES} observations.")
    if any(s.kind not in LOCATION_KINDS for s in sources):
        raise ValueError("Select location observations for this analysis.")
    if len({str(s.item.case_id) for s in sources}) > 1:
        raise ValueError("All observations must belong to the same case.")
    if len({str(s.id) for s in sources}) != len(sources):
        raise ValueError("Select each source once.")
    if not allow_alternatives and len(
        {(s.kind, str(s.item.pk)) for s in sources}
    ) != len(sources):
        raise ValueError(
            "Choose one interpretation per original observation. "
            "Use location quality to compare alternative interpretations."
        )
    return sources


def _subject(source):
    if source.kind == "person-location":
        return f"person:{source.item.person_id}"
    if source.kind == "vehicle-location":
        return f"vehicle:{source.item.vehicle_id}"
    return None


def _time(source):
    value = getattr(source.item, "observed_at", None)
    return (
        value if isinstance(value, datetime) and value.utcoffset() is not None else None
    )


def _iso(value):
    return value.astimezone(UTC).isoformat() if value else "Not recorded"


def _point(source):
    value = source.coordinates
    return coordinate(*value) if value is not None else None


def _position_kind(source):
    return (
        "Reported coordinates"
        if getattr(source.item, "latitude", None) is not None
        else "Reviewed derived coordinates"
        if source.coordinates is not None
        else "No usable coordinates"
    )


def _item(label, text, sources, **values):
    return {
        "label": label,
        "text": text,
        "source_ids": [str(s.id) for s in sources],
        "anchors": [s.anchor() for s in sources],
        "values": values,
    }


def _metrics(sources):
    def count(condition):
        return len({(s.kind, str(s.item.pk)) for s in sources if condition(s)})

    return {
        "Selected observations": count(lambda s: True),
        "Selected interpretations": len(sources),
        "With coordinates": count(lambda s: _point(s) is not None),
        "With event time": count(lambda s: _time(s) is not None),
        "Unlinked observations": count(lambda s: _subject(s) is None),
        "Method version": VERSION,
    }


def _warnings(sources, *, movement=False):
    messages = [CAUTION]
    missing = sum(_point(s) is None for s in sources)
    if missing:
        messages.append(
            f"{missing} observations have no usable coordinate interpretation."
        )
    if movement:
        messages.append(MOVEMENT_CAUTION)
        unlinked = sum(_subject(s) is None for s in sources)
        untimed = sum(_time(s) is None for s in sources)
        if unlinked:
            messages.append(
                f"{unlinked} unlinked observations are excluded from subject movement."
            )
        if untimed:
            messages.append(f"{untimed} observations have no usable event time.")
    return messages


def _clusters(sources, radius):
    "Deterministic anchor-radius groups; avoids transitive chains of places."
    groups = []
    for source in sorted(
        (s for s in sources if _point(s) is not None),
        key=lambda s: (s.kind, str(s.item.pk)),
    ):
        candidates = [
            (distance_m(_point(source), _point(g[0])), index)
            for index, g in enumerate(groups)
        ]
        nearest = min(candidates, default=(math.inf, -1))
        if nearest[0] <= radius:
            groups[nearest[1]].append(source)
        else:
            groups.append([source])
    return groups


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
    sources = _selected(sources)
    groups = defaultdict(list)
    items = []
    for source in sources:
        raw = getattr(source.item, "address", "")
        parsed = normalize_address(raw)
        items.append(
            _item(
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
            _item(
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
        metrics=_metrics(sources) | {"Candidate wording groups": len(matches)},
        warnings=[
            (
                "Identical wording and a common street name do not establish "
                "the same place. Units are preserved; unsupported "
                "international formats remain unmatched."
            )
        ],
        outcome="complete" if sources else "empty",
    )


def _source_order_discrepancies(sources):
    groups = defaultdict(list)
    for source in sources:
        if _subject(source) is not None:
            groups[_subject(source)].append(source)
    items = []
    for group in groups.values():
        # Source row order is meaningful only within the same retained export.
        imports = defaultdict(list)
        for source in group:
            if getattr(source.item, "import_batch_id", None) and getattr(
                source.item, "source_row", None
            ):
                imports[str(source.item.import_batch_id)].append(source)
        for imported in imports.values():
            ordered = sorted(imported, key=lambda s: s.item.source_row)
            for first, second in pairwise(ordered):
                if _time(first) and _time(second) and _time(second) < _time(first):
                    items.append(
                        _item(
                            "Source-row ordering discrepancy",
                            (
                                "Later export row reports an earlier event "
                                "time; originals are not reordered or "
                                "corrected."
                            ),
                            [first, second],
                        )
                    )
    return items


def location_quality(sources, parameters):
    sources = _selected(sources, allow_alternatives=True)
    radius = number(parameters, "distance_m")
    items = []
    signatures = defaultdict(list)
    for source in sources:
        item = source.item
        position, observed = _point(source), _time(source)
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
            _item(
                "Observation quality",
                "; ".join(flags)
                or "No configured discrepancy found; source accuracy remains unknown.",
                [source],
                **{
                    "Event time": _iso(observed),
                    "Receipt time": _iso(receipt),
                    "Collection time": _iso(getattr(item, "collected_at", None)),
                    "Upload time": _iso(getattr(item, "created_at", None)),
                    "Export timezone": timezone or "Not recorded",
                    "Position origin": _position_kind(source),
                    "Address": getattr(item, "address", "") or "Not recorded",
                    "Reviewed derivative coordinates": str(source.derived_coordinates)
                    if source.derived_coordinates is not None
                    else "Not selected",
                },
            )
        )
        signatures[
            (_subject(source), position, _iso(observed), getattr(item, "address", ""))
        ].append(source)
    interpretations = defaultdict(list)
    for source in sources:
        interpretations[(source.kind, str(source.item.pk))].append(source)
    for group in interpretations.values():
        if len(group) > 1:
            items.append(
                _item(
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
                _item(
                    "Repeated-observation candidate",
                    "Identical selected location/time/association fields; "
                    "independent original occurrences remain separate.",
                    group,
                    **{"Original occurrences": len(originals)},
                )
            )
    items.extend(_source_order_discrepancies(sources))
    return output(
        ("Review time semantics, positional interpretation and repeated observations."),
        items=items,
        metrics=_metrics(sources)
        | {
            "Unique original observations": len(interpretations),
            "Selected interpretations": len(sources),
        },
        warnings=_warnings(sources)
        + [
            "Alternative versions of one original are compared deliberately here. "
            "They must not be counted as independent observations or visits."
        ],
        outcome="complete" if sources else "empty",
    )


def spatial_groups(sources, parameters):
    sources = _selected(sources)
    radius = number(parameters, "distance_m")
    groups = _clusters(sources, radius)
    items = [
        _item(
            f"Spatial group {index + 1}",
            (
                "Candidate grouping within the configured radius of its first"
                " source anchor; not a visit or occupancy claim."
            ),
            group,
            **{
                "Observations": len(group),
                "Anchor latitude": _point(group[0])[0],
                "Anchor longitude": _point(group[0])[1],
                "Anchor radius (m)": radius,
                "Position origins": ", ".join(
                    sorted({_position_kind(s) for s in group})
                ),
            },
        )
        for index, group in enumerate(groups)
    ]
    return output(
        "Inspect groups of nearby observed or reviewed positions.",
        items=items,
        metrics=_metrics(sources) | {"Spatial groups": len(groups)},
        warnings=_warnings(sources)
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
    sources = _selected(sources)
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
            if reference_source is None or _point(reference_source) is None:
                raise ValueError(
                    "The reference must be a selected coordinate-bearing observation."
                )
            reference = _point(reference_source)
        else:
            reference = coordinate(
                parameters.get("reference_latitude"),
                parameters.get("reference_longitude"),
            )
        radius = number(parameters, "radius_m")
    items = []
    for source in sources:
        point = _point(source)
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
            _item(
                classification,
                (
                    "Geometric comparison; unknown accuracy can change the "
                    "classification and does not verify presence."
                ),
                [source],
                **values,
                **{"Position origin": _position_kind(source)},
            )
        )
    return output(
        "Compare selected observations with the configured radius or geofence.",
        items=items,
        metrics=_metrics(sources) | {"Compared observations": len(items)},
        warnings=_warnings(sources)
        + [
            f"Boundary ambiguity band: {boundary:g} m. Spherical distance estimates; "
            "polygon boundary distances use a local projection."
        ],
        outcome="complete" if items else "insufficient",
    )


def _tracks(sources):
    groups = defaultdict(list)
    for source in sources:
        if (
            _subject(source) is not None
            and _time(source)
            and _point(source) is not None
        ):
            groups[_subject(source)].append(source)
    return {
        key: sorted(group, key=lambda s: (_time(s), s.kind, str(s.item.pk)))
        for key, group in groups.items()
    }


def _blocks(track, gap_seconds):
    blocks = []
    for source in track:
        if (
            not blocks
            or (_time(source) - _time(blocks[-1][-1])).total_seconds() > gap_seconds
        ):
            blocks.append([source])
        else:
            blocks[-1].append(source)
    return blocks


def _gap_findings(sources, parameters):
    threshold = number(parameters, "gap_minutes") * 60
    items = []
    for subject, track in _tracks(sources).items():
        for first, second in pairwise(track):
            seconds = (_time(second) - _time(first)).total_seconds()
            if seconds > threshold:
                items.append(
                    _item(
                        "Coverage gap",
                        "This gap between usable timed positions is excluded from "
                        "visit and journey segmentation. "
                        "The endpoint samples do not establish continuous presence, "
                        "inactivity or a route between them.",
                        [first, second],
                        **{
                            "Subject association": subject,
                            "Last usable observation before gap": _iso(_time(first)),
                            "First usable observation after gap": _iso(_time(second)),
                            "Gap seconds": seconds,
                            "Maximum supported gap seconds": threshold,
                        },
                    )
                )
    return items


def _stationary_runs(block, radius):
    runs = []
    for index, source in enumerate(block):
        if runs and distance_m(_point(block[runs[-1][0]]), _point(source)) <= radius:
            runs[-1].append(index)
        else:
            runs.append([index])
    return runs


def _visits(sources, parameters):
    radius = number(parameters, "distance_m")
    gap = number(parameters, "gap_minutes") * 60
    dwell = number(parameters, "dwell_minutes") * 60
    visits = []
    for subject, track in _tracks(sources).items():
        for block in _blocks(track, gap):
            for indexes in _stationary_runs(block, radius):
                members = [block[index] for index in indexes]
                seconds = (_time(members[-1]) - _time(members[0])).total_seconds()
                if len(members) >= 2 and seconds >= dwell:
                    visits.append((subject, members, seconds))
    return visits


def candidate_visits(sources, parameters):
    sources = _selected(sources)
    radius = number(parameters, "distance_m")
    visits = _visits(sources, parameters)
    places = _clusters([members[0] for _, members, _ in visits], radius)
    place_by_id = {
        str(source.id): index + 1
        for index, group in enumerate(places)
        for source in group
    }
    items = []
    counts = Counter()
    for subject, members, seconds in visits:
        place = place_by_id[str(members[0].id)]
        counts[(subject, place)] += 1
        items.append(
            _item(
                f"Candidate visit to place {place}",
                (
                    "The observed first/last samples bound this candidate "
                    "interval; continuous presence between reports is "
                    "inferred. No arrival/departure outside these samples is "
                    "established."
                ),
                members,
                **{
                    "Subject association": subject,
                    "Place group": place,
                    "Samples": len(members),
                    "First observed": _iso(_time(members[0])),
                    "Last observed": _iso(_time(members[-1])),
                    "Observed span (seconds)": seconds,
                    "UTC weekday": _time(members[0]).strftime("%A"),
                    "UTC starting hour": _time(members[0]).hour,
                },
            )
        )
    for (subject, place), count in sorted(counts.items()):
        matching = [
            members
            for candidate_subject, members, _ in visits
            if candidate_subject == subject and place_by_id[str(members[0].id)] == place
        ]
        members = [source for group in matching for source in group]
        items.append(
            _item(
                f"Place {place} visit frequency",
                (
                    "Counted candidate visits under the configured "
                    "segmentation, not stationary sample counts. Frequent "
                    "does not imply home or workplace."
                ),
                members,
                **{
                    "Subject association": subject,
                    "Candidate visits": count,
                    "Observed samples": len(members),
                    "UTC starting hours": ", ".join(
                        str(_time(group[0]).hour) for group in matching
                    ),
                },
            )
        )
    gaps = _gap_findings(sources, parameters)
    return output(
        "Inspect candidate visits and repeated-place counts.",
        items=items + gaps,
        metrics=_metrics(sources)
        | {
            "Candidate visits": len(visits),
            "Excluded sampling gaps": len(gaps),
            "Place groups": len(places),
            "Maximum gap (minutes)": number(parameters, "gap_minutes"),
            "Minimum observed span (minutes)": number(parameters, "dwell_minutes"),
        },
        warnings=_warnings(sources, movement=True)
        + [
            (
                "Zero reported speed is not parking or engine state. A long "
                "reporting gap splits candidates rather than proving a stay."
            )
        ],
        outcome="complete" if visits else "insufficient",
    )


def _trips(sources, parameters):
    radius = number(parameters, "distance_m")
    gap = number(parameters, "gap_minutes") * 60
    dwell = number(parameters, "dwell_minutes") * 60
    trips = []
    for subject, track in _tracks(sources).items():
        for block in _blocks(track, gap):
            start = 0
            for indexes in _stationary_runs(block, radius):
                span = (
                    _time(block[indexes[-1]]) - _time(block[indexes[0]])
                ).total_seconds()
                if len(indexes) < 2 or span < dwell:
                    continue
                if indexes[0] > start and any(
                    distance_m(_point(block[start]), _point(s)) > radius
                    for s in block[start : indexes[0] + 1]
                ):
                    trips.append((subject, block[start : indexes[0] + 1]))
                start = indexes[-1]
            remainder = block[start:]
            if len(remainder) >= 2 and any(
                distance_m(_point(remainder[0]), _point(s)) > radius for s in remainder
            ):
                trips.append((subject, remainder))
    return trips


def candidate_trips(sources, parameters):
    sources = _selected(sources)
    trips = _trips(sources, parameters)
    items = []
    for subject, members in trips:
        seconds = (_time(members[-1]) - _time(members[0])).total_seconds()
        straight_segments = [
            distance_m(_point(a), _point(b)) for a, b in pairwise(members)
        ]
        items.append(
            _item(
                "Candidate journey",
                (
                    "Endpoints are first/last included observations. Straight"
                    " segments are distance estimates, not roads travelled. "
                    "Stationary samples and configured maximum gaps delimit "
                    "this candidate; unsampled departures/arrivals remain "
                    "unknown."
                ),
                members,
                **{
                    "Subject association": subject,
                    "First observed": _iso(_time(members[0])),
                    "Last observed": _iso(_time(members[-1])),
                    "Elapsed seconds": seconds,
                    "Origin latitude": _point(members[0])[0],
                    "Origin longitude": _point(members[0])[1],
                    "Destination latitude": _point(members[-1])[0],
                    "Destination longitude": _point(members[-1])[1],
                    "Observed straight-segment sum (m)": round(
                        sum(straight_segments), 2
                    ),
                    "Largest sampling interval (seconds)": max(
                        (_time(b) - _time(a)).total_seconds()
                        for a, b in pairwise(members)
                    ),
                    "Samples": len(members),
                },
            )
        )
    gaps = _gap_findings(sources, parameters)
    return output(
        "Inspect candidate journeys and drives from associated observations.",
        items=items + gaps,
        metrics=_metrics(sources)
        | {
            "Candidate journeys": len(trips),
            "Excluded sampling gaps": len(gaps),
            "Maximum gap (minutes)": number(parameters, "gap_minutes"),
        },
        warnings=_warnings(sources, movement=True),
        outcome="complete" if trips else "insufficient",
    )


def comparison_periods(parameters):
    names = ("period_a_start", "period_a_end", "period_b_start", "period_b_end")
    if not any(parameters.get(name) for name in names):
        return None
    if not all(parameters.get(name) for name in names):
        raise ValueError("Choose start and end timestamps for both comparison periods.")
    values = []
    for name in names:
        try:
            value = datetime.fromisoformat(parameters[name])
            if value.utcoffset() is None:
                raise ValueError
        except ValueError, TypeError:
            raise ValueError(
                "Comparison periods need ISO timestamps with explicit timezones."
            ) from None
        values.append(value.astimezone(UTC))
    first, second = (values[0], values[1]), (values[2], values[3])
    if any(start >= end for start, end in (first, second)):
        raise ValueError("Each comparison period must end after it starts.")
    if max(first[0], second[0]) < min(first[1], second[1]):
        raise ValueError(
            "Choose disjoint comparison periods to avoid counting samples twice."
        )
    return {"A": first, "B": second}


def _within(source, bounds):
    value = _time(source)
    return value is not None and bounds[0] <= value < bounds[1]


def _coverage_values(sources, bounds, parameters):
    timed = [s for s in sources if _within(s, bounds)]
    positioned = sorted((s for s in timed if _point(s) is not None), key=_time)
    intervals = [(_time(b) - _time(a)).total_seconds() for a, b in pairwise(positioned)]
    maximum_gap = number(parameters, "gap_minutes") * 60
    return {
        "Window start (inclusive)": _iso(bounds[0]),
        "Window end (exclusive)": _iso(bounds[1]),
        "Window duration denominator (seconds)": (
            bounds[1] - bounds[0]
        ).total_seconds(),
        "Selected timed observations": len(timed),
        "Usable timed positions": len(positioned),
        "Timed observations without coordinates": len(timed) - len(positioned),
        "First usable observation": _iso(_time(positioned[0]))
        if positioned
        else "Not recorded",
        "Last usable observation": _iso(_time(positioned[-1]))
        if positioned
        else "Not recorded",
        "Observed usable span (seconds)": (
            _time(positioned[-1]) - _time(positioned[0])
        ).total_seconds()
        if positioned
        else 0,
        "Largest sampling interval (seconds)": max(intervals)
        if intervals
        else "Unknown",
        "Excluded sampling gaps": sum(value > maximum_gap for value in intervals),
        "Summed within-threshold sample intervals (seconds)": sum(
            value for value in intervals if value <= maximum_gap
        ),
    }


def _month_bounds(value):
    start = value.astimezone(UTC).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end


def _hour_counts(matching):
    hours = Counter(_time(trip[0]).astimezone(UTC).hour for trip in matching)
    return (
        "; ".join(
            f"{hour:02d}:00 UTC: {count}" for hour, count in sorted(hours.items())
        )
        or "No observed departures"
    )


def _corridor_signatures(trips, places):
    signatures = Counter()
    for trip in trips:
        sequence = []
        for source in trip:
            place = places[str(source.id)]
            if not sequence or sequence[-1] != place:
                sequence.append(place)
        if len(sequence) >= 3:
            signatures[tuple(sequence)] += 1
    return signatures


def _routine_comparison(pair, period_trips, places, reference_sources):
    subject, origin, destination = pair
    selected = {
        name: [
            trip
            for candidate_subject, trip in trips
            if candidate_subject == subject
            and places[str(trip[0].id)] == origin
            and places[str(trip[-1].id)] == destination
        ]
        for name, trips in period_trips.items()
    }
    denominators = {
        name: sum(candidate_subject == subject for candidate_subject, _ in trips)
        for name, trips in period_trips.items()
    }
    if not any(selected.values()):
        return None
    first_count, second_count = len(selected["A"]), len(selected["B"])
    shares = {
        name: len(trips) / denominators[name] if denominators[name] else None
        for name, trips in selected.items()
    }
    if not all(denominators.values()):
        change = "Insufficient journey coverage in one comparison period"
    elif first_count == 0:
        change = "Endpoint pair newly observed in period B's selected journeys"
    elif second_count == 0:
        change = "Endpoint pair not observed in period B's selected journeys"
    elif shares["B"] > shares["A"]:
        change = "Endpoint pair has a larger share of selected journeys in period B"
    elif shares["B"] < shares["A"]:
        change = "Endpoint pair has a smaller share of selected journeys in period B"
    else:
        change = "Same endpoint-pair share among selected journeys"
    values = {"Subject association": subject, "Observed recurrence comparison": change}
    for name, trips in selected.items():
        values |= {
            f"Period {name} pair journeys": len(trips),
            f"Period {name} subject journey denominator": denominators[name],
            f"Period {name} pair share": round(shares[name], 4)
            if shares[name] is not None
            else "Unknown",
            f"Period {name} UTC departure distribution": _hour_counts(trips),
            f"Period {name} observed durations (seconds)": ", ".join(
                str((_time(t[-1]) - _time(t[0])).total_seconds()) for t in trips
            )
            or "No observed journeys",
        }
    corridors = {
        name: _corridor_signatures(trips, places) for name, trips in selected.items()
    }
    if all(corridors.values()):
        common = set(corridors["A"]) & set(corridors["B"])
        values |= {
            "Observed corridor comparison": (
                "Interior-sample spatial-group sequences; "
                "sampling density can change these sequences"
            ),
            "Shared observed sequences": len(common),
            "Sequences first observed in B": len(
                set(corridors["B"]) - set(corridors["A"])
            ),
        }
        for name, signatures in corridors.items():
            values[f"Period {name} observed group sequences"] = "; ".join(
                f"{' → '.join(str(place) for place in sequence)} ({count})"
                for sequence, count in sorted(signatures.items())
            )
    else:
        values["Observed corridor comparison"] = (
            "Insufficient interior samples in one or both periods; "
            "endpoints do not establish a corridor"
        )
    members = {
        str(s.id): s for name in selected for trip in selected[name] for s in trip
    }
    reference_places = {origin, destination}
    for signatures in corridors.values():
        reference_places.update(place for sequence in signatures for place in sequence)
    for place in reference_places:
        source = reference_sources[place]
        members[str(source.id)] = source
    values["Reference spatial-group anchor coordinates"] = "; ".join(
        f"{place}: {_point(reference_sources[place])}"
        for place in sorted(reference_places)
    )
    return _item(
        f"Place {origin} → place {destination} · period comparison",
        "Observed changes describe the selected samples, "
        "not suspicious behavior or "
        "unobserved habits. Compare period coverage and reporting density first. "
        "Interior sample groups do not reconstruct road routes. "
        "Source links include the spatial-group reference anchors.",
        list(members.values()),
        **values,
    )


def movement_routines(sources, parameters):
    sources = _selected(sources)
    periods = comparison_periods(parameters)
    trips = _trips(sources, parameters)
    # Use one spatial reference for endpoints and observed interior groups.
    groups = _clusters(sources, number(parameters, "distance_m"))
    place_by_id = {
        str(source.id): index + 1
        for index, group in enumerate(groups)
        for source in group
    }
    reference_sources = {index + 1: group[0] for index, group in enumerate(groups)}
    pairs = defaultdict(list)
    for subject, members in trips:
        pairs[
            (subject, place_by_id[str(members[0].id)], place_by_id[str(members[-1].id)])
        ].append(members)
    subject_sources = defaultdict(list)
    for source in sources:
        if _subject(source) is not None:
            subject_sources[_subject(source)].append(source)
    items = []
    for (subject, origin, destination), matching in sorted(pairs.items()):
        members = list({str(s.id): s for trip in matching for s in trip}.values())
        for place in (origin, destination):
            anchor = reference_sources[place]
            if str(anchor.id) not in {str(source.id) for source in members}:
                members.append(anchor)
        months = Counter(
            _time(trip[0]).astimezone(UTC).strftime("%Y-%m") for trip in matching
        )
        items.append(
            _item(
                f"Place {origin} → place {destination}",
                "Directed endpoint pattern among selected candidate journeys. Shared "
                "endpoints do not establish a shared route or habitual role.",
                members,
                **{
                    "Subject association": subject,
                    "Origin group anchor coordinates": str(
                        _point(reference_sources[origin])
                    ),
                    "Destination group anchor coordinates": str(
                        _point(reference_sources[destination])
                    ),
                    "Candidate journeys": len(matching),
                    "UTC departure distribution": _hour_counts(matching),
                    "Selected monthly counts": "; ".join(
                        f"{month}: {count}" for month, count in sorted(months.items())
                    ),
                    "Observed duration seconds": ", ".join(
                        str((_time(t[-1]) - _time(t[0])).total_seconds())
                        for t in matching
                    ),
                },
            )
        )
    if periods:
        period_trips = {
            name: _trips([s for s in sources if _within(s, bounds)], parameters)
            for name, bounds in periods.items()
        }
        # Boundary-truncated candidates may differ from full-selection segmentation.
        comparison_pairs = {
            (subject, place_by_id[str(trip[0].id)], place_by_id[str(trip[-1].id)])
            for candidates in period_trips.values()
            for subject, trip in candidates
        }
        for subject, selected in sorted(subject_sources.items()):
            for name, bounds in periods.items():
                usable = [s for s in selected if _within(s, bounds)]
                items.append(
                    _item(
                        f"Period {name} observation coverage",
                        "Counts use only selected sources inside this window; "
                        "missing "
                        "event times cannot be assigned to either period. "
                        "Summed sample "
                        "intervals are not continuous-presence coverage. Links include "
                        "selection context when the window contains no samples.",
                        usable or selected,
                        **{
                            "Subject association": subject,
                            **_coverage_values(selected, bounds, parameters),
                        },
                    )
                )
        for pair in sorted(comparison_pairs):
            comparison = _routine_comparison(
                pair, period_trips, place_by_id, reference_sources
            )
            if comparison is not None:
                items.append(comparison)
    else:
        for subject, selected in sorted(subject_sources.items()):
            months = {_month_bounds(_time(s)) for s in selected if _time(s)}
            for bounds in sorted(months):
                members = [s for s in selected if _within(s, bounds)]
                items.append(
                    _item(
                        "Selected calendar-month coverage",
                        "The full calendar month is the denominator; "
                        "selected samples "
                        "and their intervals do not establish "
                        "uninterrupted observation.",
                        members,
                        **{
                            "Subject association": subject,
                            **_coverage_values(selected, bounds, parameters),
                        },
                    )
                )
    gaps = _gap_findings(sources, parameters)
    return output(
        "Compare directed origins/destinations, selected coverage "
        "and observed movement patterns.",
        items=items + gaps,
        metrics=_metrics(sources)
        | {
            "Candidate journeys": len(trips),
            "Directed place pairs": len(pairs),
            "Explicit comparison periods": bool(periods),
            "Excluded sampling gaps": len(gaps),
        },
        warnings=_warnings(sources, movement=True)
        + [
            "Counts depend on selected periods, reporting density "
            "and missing observations. "
            "Period bounds are inclusive/exclusive UTC and can cut candidate journeys. "
            "Observed interior-point groups are sampling-sensitive "
            "corridor candidates, "
            "not reconstructed road routes or claims about suspicious behavior."
        ],
        outcome="complete" if trips else "insufficient",
    )


def colocation(sources, parameters):
    sources = _selected(sources)
    radius = number(parameters, "distance_m")
    window = number(parameters, "time_window_minutes") * 60
    observed = [
        source
        for source in sources
        if _subject(source) is not None and _time(source) and _point(source) is not None
    ]
    items = []
    for first, second in combinations(observed, 2):
        if _subject(first) == _subject(second):
            continue
        seconds = abs((_time(first) - _time(second)).total_seconds())
        if seconds > window:
            continue
        distance = distance_m(_point(first), _point(second))
        if distance <= radius:
            items.append(
                _item(
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
                        "First event time": _iso(_time(first)),
                        "Second event time": _iso(_time(second)),
                        "Position origins": (
                            f"{_position_kind(first)}; {_position_kind(second)}"
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
    warnings = _warnings(sources, movement=True)
    if len(items) >= 1000:
        warnings.append(
            "Candidate output is capped at 1,000 pairs; narrow the "
            "selection to inspect all possible pairs."
        )
    return output(
        "Inspect candidate co-location with source/time/position limitations.",
        items=items,
        metrics=_metrics(sources)
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


def travel_discrepancies(sources, parameters):
    sources = _selected(sources)
    speed_limit = number(parameters, "speed_limit_kmh")
    gap = number(parameters, "gap_minutes") * 60
    radius = number(parameters, "distance_m")
    items = _gap_findings(sources, parameters)
    for track in _tracks(sources).values():
        for first, second in pairwise(track):
            seconds = (_time(second) - _time(first)).total_seconds()
            distance = distance_m(_point(first), _point(second))
            if seconds > gap:
                continue
            if seconds == 0 and distance > radius:
                items.append(
                    _item(
                        "Simultaneous position discrepancy",
                        (
                            "Distant samples have the same reported time. "
                            "Review clocks, associations and coordinate "
                            "interpretations; neither observation is "
                            "corrected."
                        ),
                        [first, second],
                        **{"Distance estimate (m)": round(distance, 2)},
                    )
                )
            elif seconds > 0:
                speed = distance / seconds * 3.6
                if speed > speed_limit:
                    items.append(
                        _item(
                            "Rapid-transition discrepancy",
                            (
                                "Straight-line distance/time exceeds the "
                                "selected screening threshold. This may be a "
                                "timestamp, position, source or association "
                                "problem; it is not a transport-mode or "
                                "deception conclusion."
                            ),
                            [first, second],
                            **{
                                "Elapsed seconds": seconds,
                                "Distance estimate (m)": round(distance, 2),
                                "Required average km/h": round(speed, 2),
                                "Screening threshold km/h": speed_limit,
                            },
                        )
                    )
    # Only retained row order can support a chronology claim; selection order cannot.
    items.extend(_source_order_discrepancies(sources))
    return output(
        ("Review apparently incompatible transitions and source-order discrepancies."),
        items=items,
        metrics=_metrics(sources) | {"Reported discrepancies or gaps": len(items)},
        warnings=_warnings(sources, movement=True),
        outcome="complete" if _tracks(sources) else "insufficient",
    )


def location_conflicts(sources, parameters):
    sources = _selected(sources)
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
            "Event time": _iso(_time(source)),
            "Subject association": _subject(source) or "Unlinked observation",
            "Position origin": _position_kind(source),
            "Source citation": getattr(source.item, "source", "") or "Not recorded",
        }
        if _point(source):
            values |= {"Latitude": _point(source)[0], "Longitude": _point(source)[1]}
        if _point(source) and _point(first):
            values["Distance from first selected observation (m)"] = round(
                distance_m(_point(source), _point(first)), 2
            )
        if _time(source) and _time(first):
            values["Time difference from first selected observation (seconds)"] = (
                _time(source) - _time(first)
            ).total_seconds()
        items.append(
            _item(
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
        metrics=_metrics(sources),
        warnings=_warnings(sources)
        + [
            (
                "Accept, reject or leave the interpretation unresolved "
                "through retained review; original observations remain "
                "unchanged."
            )
        ],
    )
