"""Shared case observation selection, bounds, and finding presentation."""

import math
from collections import defaultdict
from datetime import UTC, datetime
from itertools import pairwise

from core.evidence.types import LOCATION_KINDS
from geo.coordinates import (
    coordinate,
    distance_m,
)

VERSION = "location-observations.v2"
MAX_SOURCES = 500
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


def selected_observations(sources, *, allow_alternatives=False):
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


def subject_key(source):
    if source.kind == "person-location":
        return f"person:{source.item.person_id}"
    if source.kind == "vehicle-location":
        return f"vehicle:{source.item.vehicle_id}"
    return None


def observed_time(source):
    value = getattr(source.item, "observed_at", None)
    return (
        value if isinstance(value, datetime) and value.utcoffset() is not None else None
    )


def format_observed_time(value):
    return value.astimezone(UTC).isoformat() if value else "Not recorded"


def observation_point(source):
    value = source.coordinates
    return coordinate(*value) if value is not None else None


def position_kind(source):
    return (
        "Reported coordinates"
        if getattr(source.item, "latitude", None) is not None
        else "Reviewed derived coordinates"
        if source.coordinates is not None
        else "No usable coordinates"
    )


def location_item(label, text, sources, **values):
    return {
        "label": label,
        "text": text,
        "source_ids": [str(s.id) for s in sources],
        "anchors": [s.anchor() for s in sources],
        "values": values,
    }


def observation_metrics(sources):
    def count(condition):
        return len({(s.kind, str(s.item.pk)) for s in sources if condition(s)})

    return {
        "Selected observations": count(lambda s: True),
        "Selected interpretations": len(sources),
        "With coordinates": count(lambda s: observation_point(s) is not None),
        "With event time": count(lambda s: observed_time(s) is not None),
        "Unlinked observations": count(lambda s: subject_key(s) is None),
        "Method version": VERSION,
    }


def observation_warnings(sources, *, movement=False):
    messages = [CAUTION]
    missing = sum(observation_point(s) is None for s in sources)
    if missing:
        messages.append(
            f"{missing} observations have no usable coordinate interpretation."
        )
    if movement:
        messages.append(MOVEMENT_CAUTION)
        unlinked = sum(subject_key(s) is None for s in sources)
        untimed = sum(observed_time(s) is None for s in sources)
        if unlinked:
            messages.append(
                f"{unlinked} unlinked observations are excluded from subject movement."
            )
        if untimed:
            messages.append(f"{untimed} observations have no usable event time.")
    return messages


def cluster_observations(sources, radius):
    "Deterministic anchor-radius groups; avoids transitive chains of places."
    groups = []
    for source in sorted(
        (s for s in sources if observation_point(s) is not None),
        key=lambda s: (s.kind, str(s.item.pk)),
    ):
        candidates = [
            (distance_m(observation_point(source), observation_point(g[0])), index)
            for index, g in enumerate(groups)
        ]
        nearest = min(candidates, default=(math.inf, -1))
        if nearest[0] <= radius:
            groups[nearest[1]].append(source)
        else:
            groups.append([source])
    return groups


def source_order_discrepancies(sources):
    groups = defaultdict(list)
    for source in sources:
        if subject_key(source) is not None:
            groups[subject_key(source)].append(source)
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
                if (
                    observed_time(first)
                    and observed_time(second)
                    and observed_time(second) < observed_time(first)
                ):
                    items.append(
                        location_item(
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
