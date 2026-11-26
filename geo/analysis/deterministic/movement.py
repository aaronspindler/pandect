"""Candidate visits, journeys, movement comparisons, and travel discrepancies."""

from collections import Counter, defaultdict
from datetime import UTC, datetime
from itertools import pairwise

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
    selected_observations,
    source_order_discrepancies,
    subject_key,
)
from geo.coordinates import distance_m


def _tracks(sources):
    groups = defaultdict(list)
    for source in sources:
        if (
            subject_key(source) is not None
            and observed_time(source)
            and observation_point(source) is not None
        ):
            groups[subject_key(source)].append(source)
    return {
        key: sorted(group, key=lambda s: (observed_time(s), s.kind, str(s.item.pk)))
        for key, group in groups.items()
    }


def _blocks(track, gap_seconds):
    blocks = []
    for source in track:
        if (
            not blocks
            or (observed_time(source) - observed_time(blocks[-1][-1])).total_seconds()
            > gap_seconds
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
            seconds = (observed_time(second) - observed_time(first)).total_seconds()
            if seconds > threshold:
                items.append(
                    location_item(
                        "Coverage gap",
                        "This gap between usable timed positions is excluded from "
                        "visit and journey segmentation. "
                        "The endpoint samples do not establish continuous presence, "
                        "inactivity or a route between them.",
                        [first, second],
                        **{
                            "Subject association": subject,
                            "Last usable observation before gap": format_observed_time(
                                observed_time(first)
                            ),
                            "First usable observation after gap": format_observed_time(
                                observed_time(second)
                            ),
                            "Gap seconds": seconds,
                            "Maximum supported gap seconds": threshold,
                        },
                    )
                )
    return items


def _stationary_runs(block, radius):
    runs = []
    for index, source in enumerate(block):
        if (
            runs
            and distance_m(
                observation_point(block[runs[-1][0]]), observation_point(source)
            )
            <= radius
        ):
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
                seconds = (
                    observed_time(members[-1]) - observed_time(members[0])
                ).total_seconds()
                if len(members) >= 2 and seconds >= dwell:
                    visits.append((subject, members, seconds))
    return visits


def candidate_visits(sources, parameters):
    sources = selected_observations(sources)
    radius = number(parameters, "distance_m")
    visits = _visits(sources, parameters)
    places = cluster_observations([members[0] for _, members, _ in visits], radius)
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
            location_item(
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
                    "First observed": format_observed_time(observed_time(members[0])),
                    "Last observed": format_observed_time(observed_time(members[-1])),
                    "Observed span (seconds)": seconds,
                    "UTC weekday": observed_time(members[0]).strftime("%A"),
                    "UTC starting hour": observed_time(members[0]).hour,
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
            location_item(
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
                        str(observed_time(group[0]).hour) for group in matching
                    ),
                },
            )
        )
    gaps = _gap_findings(sources, parameters)
    return output(
        "Inspect candidate visits and repeated-place counts.",
        items=items + gaps,
        metrics=observation_metrics(sources)
        | {
            "Candidate visits": len(visits),
            "Excluded sampling gaps": len(gaps),
            "Place groups": len(places),
            "Maximum gap (minutes)": number(parameters, "gap_minutes"),
            "Minimum observed span (minutes)": number(parameters, "dwell_minutes"),
        },
        warnings=observation_warnings(sources, movement=True)
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
                    observed_time(block[indexes[-1]]) - observed_time(block[indexes[0]])
                ).total_seconds()
                if len(indexes) < 2 or span < dwell:
                    continue
                if indexes[0] > start and any(
                    distance_m(observation_point(block[start]), observation_point(s))
                    > radius
                    for s in block[start : indexes[0] + 1]
                ):
                    trips.append((subject, block[start : indexes[0] + 1]))
                start = indexes[-1]
            remainder = block[start:]
            if len(remainder) >= 2 and any(
                distance_m(observation_point(remainder[0]), observation_point(s))
                > radius
                for s in remainder
            ):
                trips.append((subject, remainder))
    return trips


def candidate_trips(sources, parameters):
    sources = selected_observations(sources)
    trips = _trips(sources, parameters)
    items = []
    for subject, members in trips:
        seconds = (
            observed_time(members[-1]) - observed_time(members[0])
        ).total_seconds()
        straight_segments = [
            distance_m(observation_point(a), observation_point(b))
            for a, b in pairwise(members)
        ]
        items.append(
            location_item(
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
                    "First observed": format_observed_time(observed_time(members[0])),
                    "Last observed": format_observed_time(observed_time(members[-1])),
                    "Elapsed seconds": seconds,
                    "Origin latitude": observation_point(members[0])[0],
                    "Origin longitude": observation_point(members[0])[1],
                    "Destination latitude": observation_point(members[-1])[0],
                    "Destination longitude": observation_point(members[-1])[1],
                    "Observed straight-segment sum (m)": round(
                        sum(straight_segments), 2
                    ),
                    "Largest sampling interval (seconds)": max(
                        (observed_time(b) - observed_time(a)).total_seconds()
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
        metrics=observation_metrics(sources)
        | {
            "Candidate journeys": len(trips),
            "Excluded sampling gaps": len(gaps),
            "Maximum gap (minutes)": number(parameters, "gap_minutes"),
        },
        warnings=observation_warnings(sources, movement=True),
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
    value = observed_time(source)
    return value is not None and bounds[0] <= value < bounds[1]


def _coverage_values(sources, bounds, parameters):
    timed = [s for s in sources if _within(s, bounds)]
    positioned = sorted(
        (s for s in timed if observation_point(s) is not None), key=observed_time
    )
    intervals = [
        (observed_time(b) - observed_time(a)).total_seconds()
        for a, b in pairwise(positioned)
    ]
    maximum_gap = number(parameters, "gap_minutes") * 60
    return {
        "Window start (inclusive)": format_observed_time(bounds[0]),
        "Window end (exclusive)": format_observed_time(bounds[1]),
        "Window duration denominator (seconds)": (
            bounds[1] - bounds[0]
        ).total_seconds(),
        "Selected timed observations": len(timed),
        "Usable timed positions": len(positioned),
        "Timed observations without coordinates": len(timed) - len(positioned),
        "First usable observation": format_observed_time(observed_time(positioned[0]))
        if positioned
        else "Not recorded",
        "Last usable observation": format_observed_time(observed_time(positioned[-1]))
        if positioned
        else "Not recorded",
        "Observed usable span (seconds)": (
            observed_time(positioned[-1]) - observed_time(positioned[0])
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
    hours = Counter(observed_time(trip[0]).astimezone(UTC).hour for trip in matching)
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
                str((observed_time(t[-1]) - observed_time(t[0])).total_seconds())
                for t in trips
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
        f"{place}: {observation_point(reference_sources[place])}"
        for place in sorted(reference_places)
    )
    return location_item(
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
    sources = selected_observations(sources)
    periods = comparison_periods(parameters)
    trips = _trips(sources, parameters)
    # Use one spatial reference for endpoints and observed interior groups.
    groups = cluster_observations(sources, number(parameters, "distance_m"))
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
        if subject_key(source) is not None:
            subject_sources[subject_key(source)].append(source)
    items = []
    for (subject, origin, destination), matching in sorted(pairs.items()):
        members = list({str(s.id): s for trip in matching for s in trip}.values())
        for place in (origin, destination):
            anchor = reference_sources[place]
            if str(anchor.id) not in {str(source.id) for source in members}:
                members.append(anchor)
        months = Counter(
            observed_time(trip[0]).astimezone(UTC).strftime("%Y-%m")
            for trip in matching
        )
        items.append(
            location_item(
                f"Place {origin} → place {destination}",
                "Directed endpoint pattern among selected candidate journeys. Shared "
                "endpoints do not establish a shared route or habitual role.",
                members,
                **{
                    "Subject association": subject,
                    "Origin group anchor coordinates": str(
                        observation_point(reference_sources[origin])
                    ),
                    "Destination group anchor coordinates": str(
                        observation_point(reference_sources[destination])
                    ),
                    "Candidate journeys": len(matching),
                    "UTC departure distribution": _hour_counts(matching),
                    "Selected monthly counts": "; ".join(
                        f"{month}: {count}" for month, count in sorted(months.items())
                    ),
                    "Observed duration seconds": ", ".join(
                        str(
                            (observed_time(t[-1]) - observed_time(t[0])).total_seconds()
                        )
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
                    location_item(
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
            months = {
                _month_bounds(observed_time(s)) for s in selected if observed_time(s)
            }
            for bounds in sorted(months):
                members = [s for s in selected if _within(s, bounds)]
                items.append(
                    location_item(
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
        metrics=observation_metrics(sources)
        | {
            "Candidate journeys": len(trips),
            "Directed place pairs": len(pairs),
            "Explicit comparison periods": bool(periods),
            "Excluded sampling gaps": len(gaps),
        },
        warnings=observation_warnings(sources, movement=True)
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


def travel_discrepancies(sources, parameters):
    sources = selected_observations(sources)
    speed_limit = number(parameters, "speed_limit_kmh")
    gap = number(parameters, "gap_minutes") * 60
    radius = number(parameters, "distance_m")
    items = _gap_findings(sources, parameters)
    for track in _tracks(sources).values():
        for first, second in pairwise(track):
            seconds = (observed_time(second) - observed_time(first)).total_seconds()
            distance = distance_m(observation_point(first), observation_point(second))
            if seconds > gap:
                continue
            if seconds == 0 and distance > radius:
                items.append(
                    location_item(
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
                        location_item(
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
    items.extend(source_order_discrepancies(sources))
    return output(
        ("Review apparently incompatible transitions and source-order discrepancies."),
        items=items,
        metrics=observation_metrics(sources)
        | {"Reported discrepancies or gaps": len(items)},
        warnings=observation_warnings(sources, movement=True),
        outcome="complete" if _tracks(sources) else "insufficient",
    )
