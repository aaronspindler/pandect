"""WGS84 coordinate validation and spherical distances shared by maps and analysis."""

import math

EARTH_RADIUS_M = 6_371_008.8
WEB_MERCATOR_MAX_LATITUDE = 85.05112878


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
