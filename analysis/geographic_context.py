"""Automatic, case-scoped map context with retained external reference snapshots."""

import base64
import hashlib
import json
import math
from datetime import UTC, date, datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from django.conf import settings
from django.utils import timezone

from .errors import AnalysisError
from .http import NoRedirect
from .locations import (
    EARTH_RADIUS_M,
    _clusters,
    _iso,
    _item,
    _metrics,
    _point,
    _position_kind,
    _selected,
    _time,
    _warnings,
    distance_m,
    number,
)
from .parsers import run_parser
from .sources import output

MAX_AREAS = 25
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_FEATURES = 2_000
MAX_VERTICES = 50_000
MAX_MATCHES = 1_000
MAX_FINDING_BYTES = 1024 * 1024
MAX_WINDOW_RADIUS_M = 5_000


def _invalid_json_constant(value):
    raise ValueError("Non-finite JSON value")


def reference_time(raw, *, allow_date=False):
    "An explicit UTC instant, never a timezone inferred from a location."
    if raw in (None, ""):
        return None
    try:
        if allow_date and isinstance(raw, str) and len(raw) == 10:
            return datetime.combine(date.fromisoformat(raw), datetime.min.time(), UTC)
        if not isinstance(raw, str):
            raise ValueError
        value = datetime.fromisoformat(raw)
        if value.utcoffset() is None:
            raise ValueError
        return value.astimezone(UTC)
    except ValueError, TypeError, OverflowError:
        raise AnalysisError("map_context_invalid_reference_time") from None


def _bounded_number(parameters, name, default, minimum, maximum):
    value = parameters.get(name, default)
    if isinstance(value, bool):
        raise AnalysisError("map_context_invalid_parameters")
    try:
        value = float(value)
    except ValueError, TypeError:
        raise AnalysisError("map_context_invalid_parameters") from None
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise AnalysisError("map_context_invalid_parameters")
    return value


def _window_supported(center, radius):
    latitude, longitude = center
    angular = math.degrees(radius / EARTH_RADIUS_M)
    if abs(latitude) + angular >= 85:
        return False
    longitude_radius = angular / math.cos(math.radians(latitude))
    return -180 < longitude - longitude_radius and longitude + longitude_radius < 180


class ContextClient:
    "An explicitly configured Overpass interpreter endpoint; no network defaults."

    def __init__(self):
        self.endpoint = getattr(settings, "PANDECT_MAP_CONTEXT_URL", "").rstrip("/")
        self.name = getattr(settings, "PANDECT_MAP_CONTEXT_NAME", "")
        self.version = getattr(settings, "PANDECT_MAP_CONTEXT_VERSION", "")
        self.attribution = getattr(settings, "PANDECT_MAP_CONTEXT_ATTRIBUTION", "")
        self.user_agent = getattr(settings, "PANDECT_MAP_CONTEXT_USER_AGENT", "")
        self.timeout = min(
            30, max(1, getattr(settings, "PANDECT_MAP_CONTEXT_TIMEOUT", 10))
        )
        self.history_start = reference_time(
            getattr(settings, "PANDECT_MAP_CONTEXT_HISTORY_START", ""), allow_date=True
        )
        parsed = urlsplit(self.endpoint)
        if (
            not self.endpoint
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or len(self.endpoint) > 2_000
            or not all(
                isinstance(value, str) and 0 < len(value) <= limit
                for value, limit in (
                    (self.name, 250),
                    (self.version, 250),
                    (self.attribution, 2_000),
                    (self.user_agent, 250),
                )
            )
        ):
            raise AnalysisError("map_context_not_configured")
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def query(self, center, radius, as_of):
        if as_of is not None and as_of.microsecond:
            raise AnalysisError("map_context_invalid_reference_time")
        if as_of is not None and (
            self.history_start is None or as_of < self.history_start
        ):
            raise AnalysisError("map_context_history_unavailable")
        latitude, longitude = center
        area = f"(around:{radius:.2f},{latitude:.8f},{longitude:.8f})"
        historical = f'[date:"{as_of.strftime("%Y-%m-%dT%H:%M:%SZ")}"]' if as_of else ""
        return (
            f"[out:json][timeout:{self.timeout}][maxsize:16777216]{historical};("
            + "".join(
                f"nwr{area}[{tag}];"
                for tag in (
                    "highway",
                    "building",
                    "amenity",
                    "shop",
                    "tourism",
                    "leisure",
                    'place~"^(neighbourhood|quarter|suburb)$"',
                    'boundary="administrative"',
                )
            )
            + ");out meta geom;"
        )

    def lookup(self, query):
        request = Request(
            self.endpoint,
            data=urlencode({"data": query}).encode("ascii"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": self.user_agent,
            },
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                content = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            try:
                code = (
                    "map_context_rate_limited"
                    if error.code == 429
                    else "map_context_authentication"
                    if error.code in {401, 403}
                    else "map_context_unavailable"
                )
                raise AnalysisError(code) from None
            finally:
                error.close()
        except URLError, TimeoutError, OSError:
            raise AnalysisError("map_context_unavailable") from None
        if len(content) > MAX_RESPONSE_BYTES:
            raise AnalysisError("map_context_response_limit")
        return content


def geographic_context(sources, parameters, *, client=None, authorize=None):
    sources = _selected(sources)
    if (
        not getattr(settings, "PANDECT_MAP_CONTEXT_LOCAL", False)
        and parameters.get("external_processing") is not True
    ):
        raise AnalysisError("external_processing_confirmation_required")
    if authorize is None:
        raise AnalysisError("current_authorization_required")
    grouping = number(parameters, "distance_m")
    radius = _bounded_number(
        parameters, "context_radius_m", 500, 1, MAX_WINDOW_RADIUS_M
    )
    boundary = _bounded_number(parameters, "boundary_m", 10, 0, 100)
    as_of = reference_time(parameters.get("context_as_of"))
    if as_of and (as_of > timezone.now() or as_of.microsecond):
        raise AnalysisError("map_context_invalid_reference_time")
    groups = _clusters(sources, grouping)
    if not groups:
        return output(
            "No selected observation has usable coordinates. "
            "Review an address geocode first.",
            metrics=_metrics(sources) | {"Excluded observations": len(sources)},
            warnings=_warnings(sources),
            outcome="insufficient",
        )
    client = client or ContextClient()
    items, artifacts = [], []
    snapshot_bytes = features = vertices = displayed = finding_bytes = 0
    failures = lookups = omitted_findings = truncations = 0
    exclusions = sum(_point(source) is None for source in sources)
    partial = bool(exclusions)
    budgets_exhausted = False

    def append(item):
        nonlocal finding_bytes, partial, omitted_findings
        size = len(json.dumps(item, ensure_ascii=False).encode())
        if finding_bytes + size > MAX_FINDING_BYTES:
            partial = True
            omitted_findings += 1
            return False
        finding_bytes += size
        items.append(item)
        return True

    for index, group in enumerate(groups):
        center = _point(group[0])
        window_radius = radius + max(distance_m(center, _point(s)) for s in group)
        if (
            index >= MAX_AREAS
            or budgets_exhausted
            or window_radius > MAX_WINDOW_RADIUS_M
            or not _window_supported(center, window_radius)
        ):
            exclusions += len(group)
            partial = True
            append(
                _item(
                    "Map context excluded",
                    "Narrow this selection or its grouping/search radius. Query, "
                    "geometry and output budgets are bounded; polar and "
                    "dateline-crossing windows are unsupported.",
                    group,
                    **{
                        "Spatial group": index + 1,
                        "Window radius (m)": round(window_radius, 2),
                    },
                )
            )
            continue
        reference_key = f"context-{index + 1}"
        try:
            query = client.query(center, window_radius, as_of)
            authorize()
            lookups += 1
            retrieved_at = timezone.now().isoformat()
            content = client.lookup(query)
            authorize()
            if not isinstance(content, bytes) or len(content) > MAX_RESPONSE_BYTES:
                raise AnalysisError("map_context_response_limit")
            try:
                response = json.loads(content, parse_constant=_invalid_json_constant)
            except ValueError, UnicodeError, RecursionError:
                raise AnalysisError("map_context_invalid_response") from None
            if not isinstance(response, dict) or not isinstance(
                response.get("elements"), list
            ):
                raise AnalysisError("map_context_invalid_response")
            if snapshot_bytes + len(content) > MAX_SNAPSHOT_BYTES:
                budgets_exhausted = True
                raise AnalysisError("map_context_snapshot_limit")
            snapshot_bytes += len(content)
            metadata = {
                "reference_key": reference_key,
                "query": query,
                "source_ids": [str(source.id) for source in group],
                "endpoint": client.endpoint,
                "source_name": client.name,
                "reference_version": client.version,
                "attribution": client.attribution,
                "retrieved_at": retrieved_at,
                "requested_as_of": as_of.isoformat() if as_of else None,
                "dataset_timestamp": None,
            }
            osm_metadata = response.get("osm3s", {})
            dataset_timestamp = (
                osm_metadata.get("timestamp_osm_base")
                if isinstance(osm_metadata, dict)
                else None
            )
            if dataset_timestamp:
                try:
                    metadata["dataset_timestamp"] = reference_time(
                        dataset_timestamp
                    ).isoformat()
                except AnalysisError:
                    partial = True
            else:
                partial = True
            artifacts.append(
                {
                    "kind": "geographic_reference",
                    "source_id": str(group[0].id),
                    "content_type": "application/json",
                    "data_base64": base64.b64encode(content).decode("ascii"),
                    "metadata": metadata,
                }
            )
            if (
                as_of
                and metadata["dataset_timestamp"]
                and reference_time(metadata["dataset_timestamp"]) < as_of
            ):
                raise AnalysisError("map_context_history_unavailable")
            reference_values = {
                "Reference snapshot": reference_key,
                "Reference SHA-256": hashlib.sha256(content).hexdigest(),
                "Reference source": client.name,
                "Reference version": client.version,
                "Reference time": as_of.isoformat()
                if as_of
                else metadata["dataset_timestamp"] or "Not supplied",
                "Reference mode": "Historical map snapshot"
                if as_of
                else "Current retrieved map context",
                "Retrieved at": retrieved_at,
                "Attribution": client.attribution,
            }
            remark = response.get("remark")
            if remark:
                partial = True
            parsed = run_parser(
                "geographic_context",
                content,
                options={
                    "center": center,
                    "points": [_point(source) for source in group],
                    "radius_m": radius,
                    "boundary_m": boundary,
                    "max_features": MAX_FEATURES - features,
                    "max_vertices": MAX_VERTICES - vertices,
                    "max_matches": MAX_MATCHES - displayed,
                },
            )
            features += parsed["features"]
            vertices += parsed["vertices"]
            partial |= bool(
                parsed["truncated"]
                or parsed["ignored_features"]
                or parsed["missing_geometry"]
            )
            truncations += bool(parsed["truncated"])
            matched = set()
            for match in parsed["matches"]:
                source = group[match["point_index"]]
                item = _item(
                    f"{', '.join(match['categories'])}: {match['name']}",
                    "Candidate geographic context for this observation. Map geometry "
                    "and labels do not establish presence, occupancy, a meeting "
                    "or a driver's identity.",
                    [source],
                    **reference_values,
                    **{
                        "Spatial group": index + 1,
                        "Feature ID": match["feature_id"],
                        "Geometry type": match["geometry_type"],
                        "Spatial relation": match["relation"],
                        "Approximate distance (m)": match["distance_m"],
                        "Approximate boundary distance (m)": match[
                            "boundary_distance_m"
                        ],
                        "Ambiguity band (m)": boundary,
                        "Position origin": _position_kind(source),
                        "Observation time": _iso(_time(source)),
                        "Geometry method": parsed["version"],
                    },
                )
                item["anchors"][0] |= {
                    "reference_key": reference_key,
                    "feature_id": match["feature_id"],
                }
                if not append(item):
                    budgets_exhausted = True
                    break
                matched.add(source.id)
                displayed += 1
            unmatched = [source for source in group if source.id not in matched]
            if (
                unmatched
                or remark
                or parsed["truncated"]
                or parsed["missing_geometry"]
                or parsed["ignored_features"]
            ):
                append(
                    _item(
                        "Map reference coverage",
                        "No supported match was returned for some observations, "
                        "or the reference response has incomplete geometry/coverage. "
                        "A local query can miss large enclosing boundaries; this is "
                        "not evidence that no place or building exists.",
                        unmatched or group,
                        **reference_values,
                        **{
                            "Spatial group": index + 1,
                            "Observations without displayed matches": len(unmatched),
                            "Returned features with unavailable geometry": parsed[
                                "missing_geometry"
                            ],
                            "Ignored invalid/unsupported features": parsed[
                                "ignored_features"
                            ],
                            "Service reported incomplete response": bool(remark),
                            "Geometry/output limit reached": parsed["truncated"],
                        },
                    )
                )
            budgets_exhausted |= (
                features >= MAX_FEATURES
                or vertices >= MAX_VERTICES
                or displayed >= MAX_MATCHES
                or parsed["truncated"]
            )
        except AnalysisError as error:
            # Permission removal/cancellation must propagate to the publication
            # boundary rather than becoming a publishable partial success.
            if error.code in {
                "access_removed",
                "cancelled",
                "processing_cancelled",
                "source_changed",
                "source_unavailable",
                "geocode_review_required",
            }:
                raise
            failures += len(group)
            partial = True
            values = {"Failure code": error.code, "Spatial group": index + 1}
            if any(
                item["metadata"]["reference_key"] == reference_key for item in artifacts
            ):
                values["Reference snapshot"] = reference_key
            append(
                _item(
                    "Map context unavailable",
                    "This lookup or its geometry could not be completed within "
                    "supported bounds. Completed references remain separate; retry "
                    "through a new authorized run. Historical requests never fall "
                    "back to current map data.",
                    group,
                    **values,
                )
            )
    result = output(
        "Inspect source-backed geographic context for selected observations.",
        items=items,
        metrics=_metrics(sources)
        | {
            "Spatial groups": len(groups),
            "External lookups": lookups,
            "Retained reference snapshots": len(artifacts),
            "Retained response bytes": snapshot_bytes,
            "Processed features": features,
            "Processed vertices": vertices,
            "Displayed context matches": displayed,
            "Failed observations": failures,
            "Excluded observations": exclusions,
            "Findings omitted by display-byte limit": omitted_findings,
            "Geometry/display truncations": truncations,
        },
        warnings=_warnings(sources)
        + [
            "Map context describes the retained reference snapshot, not necessarily "
            "conditions at the observation time. Historical map state is not "
            "independent verification of real-world conditions.",
            "Local bounded queries can omit large enclosing boundaries and unmapped "
            "features. Missing matches do not establish the absence of a "
            "neighbourhood, venue, road or building. Nearby point labels have no "
            "implied area boundary.",
            "Distances use a local metric approximation. Positions and map "
            "boundaries have unknown accuracy; overlapping candidates remain "
            "distinct and shared buildings do not establish shared occupancy.",
            "Lookup, geometry and display budgets are bounded. Full returned "
            "geometry is retained only in private reference snapshots. External "
            "lookup is not ML inference and sends no case/person/vehicle identifiers.",
        ],
        outcome="partial" if partial else "complete",
    )
    result["artifacts"] = artifacts
    return result
