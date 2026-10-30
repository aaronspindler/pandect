"""Synthetic map references; no test calls an external map service."""

import base64
import hashlib
import io
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError, URLError
from uuid import UUID

from django.test import SimpleTestCase, override_settings

from analysis.errors import AnalysisError
from analysis.geographic_context import (
    MAX_RESPONSE_BYTES,
    ContextClient,
    geographic_context,
    reference_time,
)
from analysis.locations import spatial_groups
from analysis.parsers import run_parser
from analysis.sources import Source

MAP_SETTINGS = {
    "PANDECT_MAP_CONTEXT_URL": "https://maps.example.test/api/interpreter",
    "PANDECT_MAP_CONTEXT_NAME": "Synthetic map source",
    "PANDECT_MAP_CONTEXT_VERSION": "test-reference-1",
    "PANDECT_MAP_CONTEXT_ATTRIBUTION": "Synthetic fixtures, CC0",
    "PANDECT_MAP_CONTEXT_USER_AGENT": "Pandect synthetic test",
    "PANDECT_MAP_CONTEXT_TIMEOUT": 10,
    "PANDECT_MAP_CONTEXT_HISTORY_START": "2019-01-01",
}


def source(index, point=(0, 0), *, derived=False, case_id=1):
    identifier = UUID(int=100 + index)
    return Source(
        str(identifier),
        "location",
        SimpleNamespace(
            pk=identifier,
            case_id=UUID(int=case_id),
            latitude=None if derived or point is None else point[0],
            longitude=None if derived or point is None else point[1],
            observed_at=datetime(2020, 1, 2, tzinfo=UTC),
            address="Synthetic address",
        ),
        derived_coordinates=point if derived else None,
    )


def node(identifier=1, point=(0, 0), **tags):
    return {
        "type": "node",
        "id": identifier,
        "lat": point[0],
        "lon": point[1],
        "tags": tags or {"amenity": "cafe", "name": "Synthetic cafe"},
    }


def ring(points):
    return [{"lat": lat, "lon": lon} for lon, lat in points]


def response(elements=None, **values):
    return json.dumps(
        {
            "version": 0.6,
            "osm3s": {"timestamp_osm_base": "2026-10-01T00:00:00Z"},
            "elements": elements if elements is not None else [node()],
            **values,
        },
        indent=2,
    ).encode()


def fake_client(content=None):
    client = ContextClient()
    client.lookup = Mock(return_value=content if content is not None else response())
    return client


def parse(content, points, **options):
    return run_parser(
        "geographic_context",
        content,
        options={
            "center": [0, 0],
            "points": points,
            "radius_m": 500,
            "boundary_m": 10,
            "max_features": 2_000,
            "max_vertices": 50_000,
            "max_matches": 1_000,
            **options,
        },
    )


@override_settings(**MAP_SETTINGS)
class GeographicContextTests(SimpleTestCase):
    def test_exact_snapshot_and_individual_source_links(self):
        original = response(
            [
                node(amenity="cafe", name="A <script> & cafe"),
                node(2, (0, 0.001), shop="books", name="Bookshop"),
            ]
        )
        client = fake_client(original)
        authorize = Mock()
        observations = [source(1), source(2, (0, 0.0005), derived=True)]
        result = geographic_context(
            observations,
            {"external_processing": True},
            client=client,
            authorize=authorize,
        )
        self.assertEqual(result["outcome"], "complete")
        self.assertEqual(authorize.call_count, 2)
        self.assertEqual(client.lookup.call_count, 1)
        artifact = result["artifacts"][0]
        self.assertEqual(base64.b64decode(artifact["data_base64"]), original)
        self.assertEqual(artifact["kind"], "geographic_reference")
        self.assertEqual(
            artifact["metadata"]["source_ids"], [s.id for s in observations]
        )
        self.assertEqual(artifact["metadata"]["reference_key"], "context-1")
        self.assertEqual(result["metrics"]["Displayed context matches"], 4)
        self.assertTrue(all(len(item["source_ids"]) == 1 for item in result["items"]))
        self.assertEqual(
            result["items"][0]["values"]["Reference SHA-256"],
            hashlib.sha256(original).hexdigest(),
        )
        self.assertEqual(result["items"][0]["anchors"][0]["reference_key"], "context-1")
        self.assertTrue(
            any(
                item["values"]["Position origin"] == "Reviewed derived coordinates"
                for item in result["items"]
            )
        )
        self.assertIsNone(observations[1].item.latitude)

    def test_historical_query_has_no_current_fallback(self):
        client = fake_client()
        result = geographic_context(
            [source(1)],
            {"external_processing": True, "context_as_of": "2020-01-01T03:00:00+03:00"},
            client=client,
            authorize=Mock(),
        )
        query = client.lookup.call_args.args[0]
        self.assertIn('[date:"2020-01-01T00:00:00Z"]', query)
        self.assertNotIn("is_in", query)
        self.assertEqual(
            result["artifacts"][0]["metadata"]["requested_as_of"],
            "2020-01-01T00:00:00+00:00",
        )
        client.lookup.side_effect = AnalysisError("map_context_unavailable")
        result = geographic_context(
            [source(1)],
            {"external_processing": True, "context_as_of": "2020-01-01T00:00:00Z"},
            client=client,
            authorize=Mock(),
        )
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(client.lookup.call_count, 2)
        self.assertTrue(
            all(
                '[date:"2020-01-01T00:00:00Z"]' in call.args[0]
                for call in client.lookup.call_args_list
            )
        )

    def test_history_unavailable_does_not_send_request(self):
        client = fake_client()
        result = geographic_context(
            [source(1)],
            {"external_processing": True, "context_as_of": "2018-01-01T00:00:00Z"},
            client=client,
            authorize=Mock(),
        )
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(
            result["items"][0]["values"]["Failure code"],
            "map_context_history_unavailable",
        )
        client.lookup.assert_not_called()

    def test_missing_dates_remarks_and_unavailable_geometry_are_partial(self):
        client = fake_client(
            response(
                [{"type": "way", "id": 2, "tags": {"building": "yes"}}],
                osm3s={},
                remark="runtime error: incomplete",
            )
        )
        result = geographic_context(
            [source(1)], {"external_processing": True}, client=client, authorize=Mock()
        )
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(len(result["artifacts"]), 1)
        self.assertIsNone(result["artifacts"][0]["metadata"]["dataset_timestamp"])
        self.assertIn("not evidence", result["items"][0]["text"])
        self.assertTrue(
            result["items"][0]["values"]["Service reported incomplete response"]
        )

    def test_malformed_response_is_distinct_from_no_match(self):
        for content in (
            b"{broken",
            b"[]",
            b'{"elements": [], "invalid": NaN}',
            response(elements=[]),
        ):
            with self.subTest(content=content):
                result = geographic_context(
                    [source(1)],
                    {"external_processing": True},
                    client=fake_client(content),
                    authorize=Mock(),
                )
                if content == response(elements=[]):
                    self.assertEqual(len(result["artifacts"]), 1)
                    self.assertIn("not evidence", result["items"][0]["text"])
                else:
                    self.assertEqual(result["outcome"], "partial")
                    self.assertEqual(result["artifacts"], [])
                    self.assertEqual(
                        result["items"][0]["values"]["Failure code"],
                        "map_context_invalid_response",
                    )

    def test_parser_failure_retains_exact_reference(self):
        original = response()
        with patch(
            "analysis.geographic_context.run_parser",
            side_effect=AnalysisError("parser_timeout"),
        ):
            result = geographic_context(
                [source(1)],
                {"external_processing": True},
                client=fake_client(original),
                authorize=Mock(),
            )
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(
            base64.b64decode(result["artifacts"][0]["data_base64"]), original
        )
        self.assertEqual(
            result["items"][0]["values"]["Reference snapshot"], "context-1"
        )

    def test_historical_reference_cannot_postdate_available_dataset(self):
        client = fake_client(
            response(osm3s={"timestamp_osm_base": "2019-12-31T00:00:00Z"})
        )
        result = geographic_context(
            [source(1)],
            {
                "external_processing": True,
                "context_as_of": "2020-01-01T00:00:00Z",
            },
            client=client,
            authorize=Mock(),
        )
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(result["metrics"]["Displayed context matches"], 0)
        self.assertEqual(len(result["artifacts"]), 1)
        self.assertEqual(client.lookup.call_count, 1)
        self.assertEqual(
            result["items"][0]["values"]["Failure code"],
            "map_context_history_unavailable",
        )

    def test_partial_batches_and_display_byte_exclusions_remain_visible(self):
        client = fake_client()
        client.lookup.side_effect = [response(), b"bad response"]
        result = geographic_context(
            [source(1), source(2, (0, 0.02))],
            {"external_processing": True},
            client=client,
            authorize=Mock(),
        )
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(len(result["artifacts"]), 1)
        self.assertEqual(result["metrics"]["Displayed context matches"], 1)
        self.assertEqual(result["metrics"]["Failed observations"], 1)
        with patch("analysis.geographic_context.MAX_FINDING_BYTES", 1):
            result = geographic_context(
                [source(1)],
                {"external_processing": True},
                client=fake_client(),
                authorize=Mock(),
            )
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(result["metrics"]["Displayed context matches"], 0)
        self.assertGreater(
            result["metrics"]["Findings omitted by display-byte limit"], 0
        )

    def test_revocation_and_cancellation_are_not_partial_successes(self):
        for code, calls in (
            ("access_removed", 0),
            ("processing_cancelled", 1),
            ("source_changed", 1),
            ("geocode_review_required", 1),
        ):
            with self.subTest(code=code):
                client = fake_client()
                authorize = Mock(
                    side_effect=AnalysisError(code)
                    if calls == 0
                    else [None, AnalysisError(code)]
                )
                with self.assertRaisesMessage(AnalysisError, code):
                    geographic_context(
                        [source(1)],
                        {"external_processing": True},
                        client=client,
                        authorize=authorize,
                    )
                self.assertEqual(client.lookup.call_count, calls)

    def test_invalid_runtime_parameters_and_case_mix_are_rejected(self):
        for parameters in (
            {},
            {"external_processing": True, "context_radius_m": True},
            {"external_processing": True, "context_radius_m": 5001},
            {"external_processing": True, "boundary_m": 101},
            {"external_processing": True, "context_as_of": "2020-01-01"},
            {"external_processing": True, "context_as_of": "2020-01-01T00:00:00.123Z"},
            {"external_processing": True, "context_as_of": "9999-01-01T00:00:00Z"},
        ):
            with self.subTest(parameters=parameters), self.assertRaises(AnalysisError):
                geographic_context(
                    [source(1)], parameters, client=fake_client(), authorize=Mock()
                )
        with self.assertRaises(ValueError):
            geographic_context(
                [source(1), source(2, case_id=2)],
                {"external_processing": True},
                client=fake_client(),
                authorize=Mock(),
            )
        with self.assertRaisesMessage(AnalysisError, "current_authorization_required"):
            geographic_context(
                [source(1)], {"external_processing": True}, client=fake_client()
            )

    def test_address_only_selections_need_no_map_configuration(self):
        with override_settings(PANDECT_MAP_CONTEXT_URL=""):
            result = geographic_context(
                [source(1, None)], {"external_processing": True}, authorize=Mock()
            )
            self.assertEqual(result["outcome"], "insufficient")
            self.assertIn("geocode", result["summary"])
            self.assertEqual(spatial_groups([source(1)], {})["outcome"], "complete")

    def test_polar_dateline_and_too_broad_groups_are_excluded(self):
        for observations, parameters in (
            ([source(1, (85, 0))], {}),
            ([source(1, (0, 179.999))], {}),
            ([source(1), source(2, (0, 0.05))], {"distance_m": 10_000}),
        ):
            with self.subTest(observations=observations):
                client = fake_client()
                result = geographic_context(
                    observations,
                    {"external_processing": True, **parameters},
                    client=client,
                    authorize=Mock(),
                )
                self.assertEqual(result["outcome"], "partial")
                client.lookup.assert_not_called()
                self.assertEqual(
                    result["metrics"]["Excluded observations"], len(observations)
                )

    def test_area_snapshot_and_display_budgets_are_run_scoped(self):
        observations = [source(index + 1, (0, index * 0.02)) for index in range(30)]
        client = fake_client(response(elements=[]))
        result = geographic_context(
            observations, {"external_processing": True}, client=client, authorize=Mock()
        )
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(client.lookup.call_count, 25)
        self.assertEqual(result["metrics"]["Excluded observations"], 5)
        with patch("analysis.geographic_context.MAX_SNAPSHOT_BYTES", 1):
            result = geographic_context(
                [source(1), source(2, (0, 0.02))],
                {"external_processing": True},
                client=fake_client(),
                authorize=Mock(),
            )
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(result["artifacts"], [])
        with patch("analysis.geographic_context.MAX_MATCHES", 1):
            result = geographic_context(
                [source(1), source(2)],
                {"external_processing": True},
                client=fake_client(),
                authorize=Mock(),
            )
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(result["metrics"]["Displayed context matches"], 1)


@override_settings(**MAP_SETTINGS)
class MapClientTests(SimpleTestCase):
    def test_no_default_endpoint_and_unsafe_configuration(self):
        for endpoint in (
            "",
            "ftp://maps.example.test",
            "https://user:secret@maps.example.test",
            "https://maps.example.test/?token=secret",
            "https://maps.example.test/#fragment",
        ):
            with (
                self.subTest(endpoint=endpoint),
                override_settings(PANDECT_MAP_CONTEXT_URL=endpoint),
                self.assertRaisesMessage(AnalysisError, "map_context_not_configured"),
            ):
                ContextClient()
        with override_settings(PANDECT_MAP_CONTEXT_HISTORY_START=""):
            with self.assertRaisesMessage(
                AnalysisError, "map_context_history_unavailable"
            ):
                ContextClient().query((0, 0), 500, datetime(2020, 1, 1, tzinfo=UTC))
        self.assertEqual(
            reference_time("2020-01-01", allow_date=True),
            datetime(2020, 1, 1, tzinfo=UTC),
        )

    def test_post_body_is_fixed_query_without_case_identifiers(self):
        client = ContextClient()
        client.opener = MagicMock()
        client.opener.open.return_value.__enter__.return_value.read.return_value = (
            response()
        )
        query = client.query((43.65, -79.38), 500, None)
        original = client.lookup(query)
        request = client.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, MAP_SETTINGS["PANDECT_MAP_CONTEXT_URL"])
        self.assertIn(b"around%3A500.00%2C43.65000000%2C-79.38000000", request.data)
        self.assertNotIn(b"case", request.data)
        self.assertNotIn(b"vehicle", request.data)
        self.assertEqual(original, response())
        self.assertEqual(client.opener.open.call_args.kwargs["timeout"], 10)

    def test_safe_network_errors_and_response_limit(self):
        errors = [
            (
                HTTPError(
                    "https://secret",
                    code,
                    "private response",
                    {},
                    io.BytesIO(b"private"),
                ),
                expected,
            )
            for code, expected in (
                (429, "map_context_rate_limited"),
                (403, "map_context_authentication"),
                (500, "map_context_unavailable"),
            )
        ]
        errors.append((URLError("private input details"), "map_context_unavailable"))
        for error, expected in errors:
            with self.subTest(expected=expected):
                client = ContextClient()
                client.opener = Mock()
                client.opener.open.side_effect = error
                with self.assertRaisesMessage(AnalysisError, expected):
                    client.lookup("fixed query")
                if isinstance(error, HTTPError):
                    self.assertTrue(error.fp.closed)
        client = ContextClient()
        client.opener = MagicMock()
        client.opener.open.return_value.__enter__.return_value.read.return_value = (
            b"x" * (MAX_RESPONSE_BYTES + 1)
        )
        with self.assertRaisesMessage(AnalysisError, "map_context_response_limit"):
            client.lookup("fixed query")


class ReferenceGeometryTests(SimpleTestCase):
    def test_dateline_geometry_cannot_become_false_containment(self):
        element = {
            "type": "way",
            "id": 8,
            "tags": {"building": "yes"},
            "geometry": ring([(-179, -1), (179, -1), (179, 1), (-179, 1), (-179, -1)]),
        }
        result = parse(response([element]), [[0, 0]])
        self.assertEqual(result["matches"], [])
        self.assertGreater(result["ignored_features"], 0)

    def test_road_distance_and_point_labels_are_qualified(self):
        elements = [
            {
                "type": "way",
                "id": 1,
                "tags": {"highway": "residential", "name": "Example Road"},
                "geometry": ring([(-0.001, 0.0002), (0.001, 0.0002)]),
            },
            node(2, place="neighbourhood", name="Example Quarter"),
        ]
        result = parse(response(elements), [[0, 0]])
        road = next(x for x in result["matches"] if x["feature_id"] == "way/1")
        label = next(x for x in result["matches"] if x["feature_id"] == "node/2")
        self.assertAlmostEqual(road["distance_m"], 22.24, delta=0.1)
        self.assertEqual(label["relation"], "Nearby map label; no boundary supplied")

    def test_multipolygon_holes_and_overlapping_boundaries(self):
        outer = ring(
            [
                (-0.001, -0.001),
                (0.001, -0.001),
                (0.001, 0.001),
                (-0.001, 0.001),
                (-0.001, -0.001),
            ]
        )
        inner = ring(
            [
                (-0.0001, -0.0001),
                (0.0001, -0.0001),
                (0.0001, 0.0001),
                (-0.0001, 0.0001),
                (-0.0001, -0.0001),
            ]
        )
        elements = [
            {
                "type": "relation",
                "id": 3,
                "tags": {"type": "multipolygon", "building": "apartments"},
                "members": [
                    {"type": "way", "ref": 31, "role": "outer", "geometry": outer},
                    {"type": "way", "ref": 32, "role": "inner", "geometry": inner},
                ],
            },
            {
                "type": "way",
                "id": 4,
                "tags": {"boundary": "administrative", "name": "Example Neighbourhood"},
                "geometry": outer,
            },
        ]
        result = parse(response(elements), [[0, 0], [0.0004, 0.0004], [0.001, 0]])
        building = [x for x in result["matches"] if x["feature_id"] == "relation/3"]
        self.assertEqual(building[0]["relation"], "Nearby feature")
        self.assertEqual(building[1]["relation"], "Inside retrieved polygon")
        self.assertEqual(
            building[2]["relation"], "Near boundary; containment uncertain"
        )
        self.assertTrue(
            any(
                x["point_index"] == 1 and x["feature_id"] == "way/4"
                for x in result["matches"]
            )
        )
        self.assertEqual(result["missing_geometry"], 0)

    def test_invalid_missing_and_bounded_geometry(self):
        invalid = {
            "type": "way",
            "id": 4,
            "tags": {"building": "yes"},
            "geometry": ring([(0, 0), (0.001, 0.001), (0.001, 0), (0, 0.001), (0, 0)]),
        }
        result = parse(
            response([invalid, {"type": "way", "id": 5, "tags": {"building": "yes"}}]),
            [[0, 0]],
        )
        self.assertGreater(result["ignored_features"] + result["missing_geometry"], 0)
        with self.assertRaisesMessage(AnalysisError, "map_context_vertex_limit"):
            parse(response([node()]), [[0, 0]], max_vertices=0)
        result = parse(response([node(), node(2)]), [[0, 0]], max_features=1)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["features"], 1)
