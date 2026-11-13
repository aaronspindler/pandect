"""Location results use the recorded positions and measurements on the page."""

from core.analysis.presentation import prioritize_measurements, table_rows

PRIMARY_FIELDS = {
    "normalize_addresses": ("Original", "Normalized", "Unit", "Matching observations"),
    "location_quality": (
        "Event time",
        "Receipt time",
        "Position origin",
        "Address",
        "Reviewed derivative coordinates",
    ),
    "spatial_groups": (
        "Anchor latitude",
        "Anchor longitude",
        "Member observations",
        "Observations",
    ),
    "proximity": (
        "Distance estimate (m)",
        "Distance (m)",
        "Spatial relation",
        "Classification",
        "Position origin",
    ),
    "candidate_visits": (
        "First observed",
        "Last observed",
        "Observed span (seconds)",
        "Samples",
        "Candidate visits",
    ),
    "candidate_trips": (
        "First observed",
        "Last observed",
        "Elapsed seconds",
        "Observed straight-segment sum (m)",
        "Largest sampling interval (seconds)",
    ),
    "movement_routines": (
        "Subject association",
        "Candidate journeys",
        "Observed recurrence comparison",
        "Period A pair journeys",
        "Period B pair journeys",
        "Selected timed observations",
        "Usable timed positions",
    ),
    "colocation": (
        "Distance estimate (m)",
        "Time difference (seconds)",
        "First event time",
        "Second event time",
    ),
    "travel_discrepancies": (
        "First observed",
        "Last observed",
        "Elapsed seconds",
        "Distance estimate (m)",
        "Required average km/h",
    ),
    "location_conflicts": (
        "Address",
        "Event time",
        "Subject association",
        "Position origin",
        "Distance from first selected observation (m)",
        "Time difference from first selected observation (seconds)",
    ),
    "geocode_locations": ("Address", "Latitude", "Longitude", "Granularity"),
    "geographic_context": (
        "Spatial relation",
        "Approximate distance (m)",
        "Reference snapshot",
    ),
}


def present(operation, items, formatter):
    for item in items:
        prioritize_measurements(item, *PRIMARY_FIELDS[operation], "Gap seconds")
        if (
            operation in {"colocation", "travel_discrepancies"}
            and len(item["sources"]) == 2
        ):
            item["comparison_sources"] = item["sources"]
    return [{"items": items, "compare": operation == "location_conflicts"}]


def map_points(operation, items):
    points = {}
    for item in items:
        for source in item["sources"]:
            if source["coordinates"] is not None:
                reviewed = source["coordinate_label"] == "Reviewed estimate"
                points[source["id"]] = (
                    source["coordinates"],
                    source["label"],
                    source["url"]
                    if reviewed
                    else source["original_url"] or source["url"],
                    "reviewed" if reviewed else "reported",
                )
            if source["reviewed_coordinates"] is not None:
                points[(source["id"], "reviewed")] = (
                    source["reviewed_coordinates"],
                    source["label"],
                    source["url"],
                    "reviewed",
                )
        if operation == "geocode_locations":
            values = item.get("values", {})
            lat, lon = values.get("latitude"), values.get("longitude")
            if (
                type(lat) in (int, float)
                and type(lon) in (int, float)
                and -90 <= lat <= 90
                and -180 <= lon <= 180
            ):
                review = item.get("latest_review")
                points[item["index"]] = (
                    (lat, lon),
                    values.get("address") or item["label"],
                    item["url"],
                    "reviewed"
                    if review and review.decision == "accepted"
                    else "candidate",
                )
    if not points:
        return []
    latitudes, longitudes = zip(*(entry[0] for entry in points.values()), strict=True)
    low_lat, high_lat = min(latitudes), max(latitudes)
    low_lon, high_lon = min(longitudes), max(longitudes)
    return [
        {
            "x": 30 + 540 * (lon - low_lon) / (high_lon - low_lon)
            if high_lon > low_lon
            else 300,
            "y": 150 - 120 * (lat - low_lat) / (high_lat - low_lat)
            if high_lat > low_lat
            else 90,
            "label": label,
            "url": url,
            "kind": kind,
        }
        for (lat, lon), label, url, kind in points.values()
    ]


def report_table(data, name):
    items = sorted(data["items"], key=lambda item: item["values"]["Observed at (UTC)"])
    columns = (
        ("Observation", ("Vehicle", "Observed at (UTC)", "Address", "CSV row")),
        ("Movement", ("Reported speed (km/h)", "Raw heading", "Raw trigger"))
        if name == "vehicle-speed"
        else (
            "Timing and coverage",
            (
                "Received at (UTC)",
                "Receipt minus event (seconds)",
                "Interval since preceding timed observation (seconds)",
            ),
        ),
    )
    return [{"items": items, "columns": table_rows(items, columns)}]
