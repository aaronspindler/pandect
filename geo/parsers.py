"""Bounded OSM geometry conversion, called only in the parser child process.

The module deliberately has no Django, storage or network integration. Findings
contain small measurements and feature references, never full geometries.
"""

import json
import math
from importlib.metadata import version

from core.parser_contract import ParserFailure

EARTH_RADIUS_M = 6_371_008.8
MAX_ELEMENTS = 10_000
MAX_FEATURES = 2_000
MAX_VERTICES = 50_000
MAX_MATCHES = 1_000


def _finite(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _vertex_count(value):
    "Count returned coordinates before asking the geometry library to assemble them."
    count = 0
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if "lat" in item and "lon" in item:
                if not (_finite(item["lat"]) and _finite(item["lon"])):
                    raise ValueError("map_context_invalid_geometry")
                if not -90 <= item["lat"] <= 90 or not -180 <= item["lon"] <= 180:
                    raise ValueError("map_context_invalid_geometry")
                count += 1
            pending.extend(
                v for k, v in item.items() if k in {"elements", "members", "geometry"}
            )
        elif isinstance(item, list):
            pending.extend(item)
        if count > MAX_VERTICES:
            raise ValueError("map_context_vertex_limit")
    return count


def _categories(tags):
    result = []
    if tags.get("highway"):
        result.append("Road")
    if tags.get("building") and tags["building"] != "no":
        result.append("Building")
    if any(tags.get(key) for key in ("amenity", "shop", "tourism", "leisure")):
        result.append("Venue")
    if (
        tags.get("place") in {"neighbourhood", "quarter", "suburb"}
        or tags.get("boundary") == "administrative"
    ):
        result.append("Neighbourhood/boundary")
    return result


def extract_context(content, options):
    "Convert a bounded response and match each supplied observation independently."
    import osm2geojson
    import shapely
    from shapely.geometry import Point
    from shapely.ops import transform

    value = json.loads(content)
    elements = value.get("elements") if isinstance(value, dict) else None
    if not isinstance(elements, list) or len(elements) > MAX_ELEMENTS:
        raise ValueError("map_context_element_limit")
    vertices = _vertex_count(value)
    max_features = options.get("max_features", MAX_FEATURES)
    max_vertices = options.get("max_vertices", MAX_VERTICES)
    max_matches = options.get("max_matches", MAX_MATCHES)
    if vertices > max_vertices:
        raise ValueError("map_context_vertex_limit")
    points = options["points"]
    center = options["center"]
    radius = options["radius_m"]
    boundary = options["boundary_m"]
    if (
        not isinstance(points, list)
        or len(points) > 500
        or not 0 <= max_features <= MAX_FEATURES
        or not 0 <= max_matches <= MAX_MATCHES
        or not 0 <= max_vertices <= MAX_VERTICES
    ):
        raise ValueError("map_context_invalid_geometry")

    # Small supported windows use a local equirectangular metric approximation.
    # The caller rejects polar/dateline windows; these are context measurements,
    # not surveying distances or estimates of a receiver's accuracy.
    latitude, longitude = center
    scale = math.cos(math.radians(latitude))

    def project(x, y, z=None):
        return (
            math.radians(x - longitude) * EARTH_RADIUS_M * scale,
            math.radians(y - latitude) * EARTH_RADIUS_M,
        )

    shapes = osm2geojson.json2shapes(value, filter_used_refs=True, log_level="CRITICAL")
    features = []
    matches = []
    ignored = 0
    truncated = False
    normalized_vertices = 0
    for item in shapes:
        geometry = item.get("shape")
        properties = item.get("properties", {})
        tags = properties.get("tags", {})
        if not isinstance(tags, dict):
            ignored += 1
            continue
        categories = _categories(tags)
        if not categories:
            continue
        if len(features) >= max_features:
            truncated = True
            break
        feature_type, feature_id = properties.get("type"), properties.get("id")
        if (
            feature_type not in {"node", "way", "relation"}
            or not isinstance(feature_id, int)
            or isinstance(feature_id, bool)
            or feature_id <= 0
            or geometry is None
            or geometry.is_empty
            or not geometry.is_valid
            or geometry.geom_type
            not in {"Point", "LineString", "MultiLineString", "Polygon", "MultiPolygon"}
        ):
            ignored += 1
            continue
        coordinates = shapely.get_coordinates(geometry)
        if len(coordinates) > max_vertices - normalized_vertices or not all(
            _finite(float(x)) and _finite(float(y)) for x, y in coordinates
        ):
            truncated = True
            break
        # Reject geometries spanning the dateline rather than projecting them
        # across the globe and producing a false containment result.
        if geometry.bounds[2] - geometry.bounds[0] > 180 or any(
            abs(float(x) - longitude) > 180 for x, _ in coordinates
        ):
            ignored += 1
            continue
        normalized_vertices += len(coordinates)
        metric = transform(project, geometry)
        if not metric.is_valid:
            ignored += 1
            continue
        reference = f"{feature_type}/{feature_id}"
        features.append(reference)
        label = tags.get("name") or tags.get("ref") or "Unnamed map feature"
        label = str(label)[:250]
        is_polygon = geometry.geom_type in {"Polygon", "MultiPolygon"}
        is_point = geometry.geom_type == "Point"
        for point_index, (lat, lon) in enumerate(points):
            observation = Point(*project(lon, lat))
            distance = metric.distance(observation)
            if distance > radius + boundary:
                continue
            inside = is_polygon and metric.covers(observation)
            edge_distance = (
                metric.boundary.distance(observation) if is_polygon else distance
            )
            if len(matches) >= max_matches:
                truncated = True
                break
            relation = (
                "Near boundary; containment uncertain"
                if is_polygon and edge_distance <= boundary
                else "Inside retrieved polygon"
                if inside
                else "Nearby map label; no boundary supplied"
                if is_point and "Neighbourhood/boundary" in categories
                else "Nearby feature"
            )
            matches.append(
                {
                    "point_index": point_index,
                    "feature_id": reference,
                    "name": label,
                    "categories": categories,
                    "geometry_type": geometry.geom_type,
                    "relation": relation,
                    "distance_m": round(distance, 2),
                    "boundary_distance_m": round(edge_distance, 2)
                    if is_polygon
                    else None,
                }
            )
        if truncated:
            break
    # A tagged returned object with no assembled geometry is an explicit gap.
    expected = {
        f"{item.get('type')}/{item.get('id')}"
        for item in elements
        if isinstance(item, dict)
        and isinstance(item.get("tags"), dict)
        and _categories(item["tags"])
    }
    missing_geometry = len(expected - set(features)) if not truncated else 0
    return {
        "features": len(features),
        "vertices": max(vertices, normalized_vertices),
        "matches": matches,
        "ignored_features": ignored,
        "missing_geometry": missing_geometry,
        "truncated": truncated,
        "version": (
            f"osm2geojson {version('osm2geojson')}; Shapely {shapely.__version__}"
        ),
    }


def parse(operation, content, options):
    try:
        return extract_context(content, options)
    except ValueError as error:
        if str(error).startswith("map_context_"):
            raise ParserFailure(str(error)) from None
        raise
