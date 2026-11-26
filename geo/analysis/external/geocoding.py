"Explicit operator-configured Nominatim-compatible lookup, with no public default."

import json
import math
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from django.conf import settings
from django.utils import timezone

from core.analysis.http import NoRedirect
from core.analysis.sources import output
from geo.analysis.deterministic.observations import (
    location_item,
    selected_observations,
)
from geo.coordinates import coordinate

MAX_RESPONSE_BYTES = 256 * 1024
MAX_LOOKUPS = 25


def _search_query(address):
    """Express an imported Canadian country code as a Nominatim filter."""
    parts = address.split(",")
    if (
        len(parts) >= 5
        and all(part.strip() for part in parts)
        and parts[-2].strip().upper() == "CAN"
        and re.fullmatch(r"[A-Z][0-9][A-Z] ?[0-9][A-Z][0-9]", parts[-1].strip().upper())
    ):
        return {"q": ",".join(parts[:-2] + parts[-1:]), "countrycodes": "ca"}
    return {"q": address}


class GeocodingError(ValueError):
    "Stable safe error, excluding upstream bodies, URLs and input addresses."

    def __init__(self, code):
        self.code = code
        super().__init__(code)


class Geocoder:
    def __init__(self):
        self.endpoint = getattr(settings, "PANDECT_GEOCODER_URL", "").rstrip("/")
        self.name = getattr(settings, "PANDECT_GEOCODER_NAME", "")
        self.version = getattr(settings, "PANDECT_GEOCODER_VERSION", "")
        self.attribution = getattr(settings, "PANDECT_GEOCODER_ATTRIBUTION", "")
        self.user_agent = getattr(settings, "PANDECT_GEOCODER_USER_AGENT", "")
        self.timeout = min(
            30, max(1, getattr(settings, "PANDECT_GEOCODER_TIMEOUT", 10))
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
            or not all((self.name, self.version, self.attribution, self.user_agent))
        ):
            raise GeocodingError("geocoder_not_configured")
        # The public service is not an appropriate default batch evidence processor.
        if parsed.hostname.lower().rstrip(".") == "nominatim.openstreetmap.org":
            raise GeocodingError("public_geocoder_not_supported")
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def lookup(self, *, address=None, coordinates=None):
        if (address is None) == (coordinates is None):
            raise GeocodingError("invalid_lookup")
        query = {"format": "jsonv2", "addressdetails": 1}
        if address is not None:
            if (
                not isinstance(address, str)
                or not address.strip()
                or len(address) > 2_000
            ):
                raise GeocodingError("unsupported_address")
            query |= _search_query(address) | {"limit": 5}
            path = "search"
        else:
            lat, lon = coordinate(*coordinates)
            query |= {"lat": lat, "lon": lon}
            path = "reverse"
        request = Request(
            f"{self.endpoint}/{path}?{urlencode(query)}",
            headers={"Accept": "application/json", "User-Agent": self.user_agent},
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            try:
                if exc.code == 429:
                    code = "geocoder_rate_limited"
                elif exc.code in {401, 403}:
                    code = "geocoder_authentication_failed"
                elif exc.code == 404 and coordinates is not None:
                    return []
                else:
                    code = "geocoder_unavailable"
                raise GeocodingError(code) from None
            finally:
                exc.close()
        except URLError, TimeoutError, OSError:
            raise GeocodingError("geocoder_unavailable") from None
        if len(body) > MAX_RESPONSE_BYTES:
            raise GeocodingError("geocoder_response_too_large")
        try:
            result = json.loads(body)
        except ValueError, UnicodeError:
            raise GeocodingError("geocoder_invalid_response") from None
        if coordinates is not None:
            result = (
                [] if isinstance(result, dict) and result.get("error") else [result]
            )
        if not isinstance(result, list) or len(result) > 5:
            raise GeocodingError("geocoder_invalid_response")
        candidates = []
        for candidate in result:
            if not isinstance(candidate, dict):
                raise GeocodingError("geocoder_invalid_response")
            try:
                latitude, longitude = coordinate(
                    candidate.get("lat"), candidate.get("lon")
                )
            except ValueError:
                raise GeocodingError("geocoder_invalid_response") from None
            address = candidate.get("display_name", "")
            if not isinstance(address, str) or len(address) > 20_000:
                raise GeocodingError("geocoder_invalid_response")
            bounding = candidate.get("boundingbox", [])
            if not isinstance(bounding, list) or len(bounding) not in {0, 4}:
                raise GeocodingError("geocoder_invalid_response")
            try:
                bounding = [float(value) for value in bounding]
            except TypeError, ValueError:
                raise GeocodingError("geocoder_invalid_response") from None
            if not all(math.isfinite(value) for value in bounding):
                raise GeocodingError("geocoder_invalid_response")
            if bounding:
                try:
                    coordinate(bounding[0], bounding[2])
                    coordinate(bounding[1], bounding[3])
                except ValueError:
                    raise GeocodingError("geocoder_invalid_response") from None
                if bounding[0] > bounding[1]:
                    raise GeocodingError("geocoder_invalid_response")
            candidates.append(
                {
                    "latitude": latitude,
                    "longitude": longitude,
                    "address": address,
                    "granularity": str(
                        candidate.get("addresstype")
                        or candidate.get("type")
                        or "Unknown"
                    )[:100],
                    "bounding_box": bounding,
                    "reference_id": (
                        f"{str(candidate.get('osm_type', ''))[:20]}:"
                        f"{str(candidate.get('osm_id', ''))[:30]}"
                    ),
                    "licence": str(candidate.get("licence", self.attribution))[:2_000],
                }
            )
        return candidates


def geocode_locations(sources, parameters, *, client=None, authorize=None):
    sources = selected_observations(sources)
    if (
        not getattr(settings, "PANDECT_GEOCODER_LOCAL", False)
        and parameters.get("external_processing") is not True
    ):
        raise GeocodingError("external_processing_confirmation_required")
    if authorize is None:
        raise GeocodingError("current_authorization_required")
    reverse = parameters.get("reverse", False)
    if not isinstance(reverse, bool):
        raise GeocodingError("invalid_lookup")
    queries = {}
    skipped = []
    for source in sources:
        if reverse and source.coordinates is not None:
            key = ("reverse", coordinate(*source.coordinates))
        elif not reverse and getattr(source.item, "address", "").strip():
            key = ("search", source.item.address)
        else:
            skipped.append(source)
            continue
        queries.setdefault(key, []).append(source)
    if len(queries) > MAX_LOOKUPS:
        raise GeocodingError("geocoder_selection_too_large")
    if queries and client is None:
        client = Geocoder()
    items = []
    failures = 0
    candidates_count = 0
    for (direction, query), members in queries.items():
        # Caller rechecks the current requesting actor and case immediately here.
        authorize()
        timestamp = timezone.now().isoformat()
        lookup = {
            "Lookup direction": direction,
            "Lookup query": str(query),
            "Lookup time": timestamp,
        }
        if direction == "search":
            search = _search_query(query)
            lookup["Lookup query"] = search["q"]
            if "countrycodes" in search:
                lookup |= {
                    "Original address": query,
                    "Country filter": search["countrycodes"],
                    "Query adjustment": (
                        "The reported CAN country code is submitted as a "
                        "Canada-only country filter; other address fields are retained."
                    ),
                }
        try:
            candidates = client.lookup(
                **(
                    {"address": query}
                    if direction == "search"
                    else {"coordinates": query}
                )
            )
        except GeocodingError as exc:
            failures += len(members)
            items.append(
                location_item(
                    "Lookup unavailable",
                    (
                        "Completed lookups are retained; this lookup did not produce "
                        "usable candidates. Retry through a new authorized run."
                    ),
                    members,
                    **lookup,
                    **{"Failure code": exc.code},
                )
            )
            continue
        authorize()
        if not candidates:
            items.append(
                location_item(
                    "No geocode match",
                    "No candidate returned. "
                    "The original address/coordinates remain unchanged.",
                    members,
                    **lookup,
                    **{
                        "Lookup source": client.name,
                        "Reference version": client.version,
                    },
                )
            )
        for index, candidate in enumerate(candidates):
            candidates_count += 1
            items.append(
                location_item(
                    f"Geocode candidate {index + 1}",
                    (
                        "Derived estimate requiring investigator review before "
                        "downstream use. A returned position may be a centroid or"
                        " nearby map object, not a measured observation."
                    ),
                    members,
                    **candidate,
                    **lookup,
                    **{
                        "Lookup source": client.name,
                        "Reference version": client.version,
                        "Attribution": client.attribution,
                        "Review": "Unresolved",
                        "Alternative candidates": len(candidates),
                    },
                )
            )
    for source in skipped:
        items.append(
            location_item(
                "Lookup unsupported",
                (
                    "This selection has no address for forward lookup or no "
                    "coordinates for reverse lookup."
                ),
                [source],
            )
        )
    return output(
        "Review geocode candidates while retaining original observations.",
        items=items,
        metrics={
            "Selected observations": len(sources),
            "Unique lookups": len(queries),
            "Candidate matches": candidates_count,
            "Failed observations": failures,
            "Unsupported observations": len(skipped),
        },
        warnings=[
            (
                "Geocoding submits selected location data to the "
                "operator-configured service. Match granularity is not positional"
                " accuracy; reverse lookup identifies a nearby indexed object. "
                "Review candidates individually before use."
            )
        ],
        outcome="insufficient"
        if not queries
        else "partial"
        if failures or skipped
        else "complete",
    )
