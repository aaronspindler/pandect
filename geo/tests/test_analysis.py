"""Synthetic boundary checks for deterministic location operations and lookups."""

import io
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

from django.test import SimpleTestCase, override_settings

from core.analysis.sources import Source
from geo.analysis.deterministic.locations import (
    candidate_trips,
    candidate_visits,
    colocation,
    distance_m,
    location_conflicts,
    location_quality,
    movement_routines,
    normalize_addresses,
    proximity,
    spatial_groups,
    travel_discrepancies,
)
from geo.analysis.external.geocoding import (
    MAX_RESPONSE_BYTES,
    Geocoder,
    GeocodingError,
    geocode_locations,
)
from geo.analysis.presentation import map_points

START = datetime(2026, 10, 1, 10, tzinfo=UTC)
CASE = UUID("00000000-0000-4000-8000-000000000001")
PERSON = UUID("00000000-0000-4000-8000-000000000002")
VEHICLE = UUID("00000000-0000-4000-8000-000000000003")


def source(index, *, point=(43.65, -79.38), minute=0, kind="person-location", **values):
    identifier = UUID(int=index + 100)
    defaults = {
        "pk": identifier,
        "case_id": CASE,
        "person_id": PERSON,
        "vehicle_id": VEHICLE,
        "latitude": point[0] if point is not None else None,
        "longitude": point[1] if point is not None else None,
        "observed_at": START + timedelta(minutes=minute)
        if minute is not None
        else None,
        "address": "1 Main St, Toronto",
        "source": "Synthetic source",
        "created_at": START,
        "collected_at": None,
        "received_at": None,
        "import_batch_id": None,
        "source_row": None,
    }
    defaults.update(values)
    return Source(str(identifier), kind, SimpleNamespace(**defaults))


class LocationOperationTests(SimpleTestCase):
    def test_map_links_reported_and_reviewed_points_to_their_exact_sources(self):
        source = {
            "id": "observation",
            "label": "Retained observation",
            "coordinates": (0, 0),
            "coordinate_label": "Reported coordinates",
            "reviewed_coordinates": (0, 1),
            "url": "/analysis/lookup/?finding=3#finding-3",
            "original_url": "/evidence/observation/",
        }
        points = map_points("location_quality", [{"sources": [source]}])
        self.assertEqual(
            {point["kind"]: point["url"] for point in points},
            {
                "reported": "/evidence/observation/",
                "reviewed": "/analysis/lookup/?finding=3#finding-3",
            },
        )

    def test_geodesic_zero_dateline_and_poles(self):
        self.assertEqual(distance_m((0, 0), (0, 0)), 0)
        self.assertAlmostEqual(distance_m((0, 179.9), (0, -179.9)), 22_239, delta=10)
        self.assertAlmostEqual(distance_m((90, 0), (90, 180)), 0, delta=0.001)

    def test_unsafe_parameters_and_cross_case_inputs_are_rejected(self):
        for value in ("nan", float("inf"), -1, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                spatial_groups([source(1)], {"distance_m": value})
        with self.assertRaises(ValueError):
            spatial_groups([source(1), source(2, case_id=uuid4())], {})
        with self.assertRaises(ValueError):
            spatial_groups([source(1)] * 501, {})
        with self.assertRaises(ValueError):
            spatial_groups([source(1, point=(91, 0))], {})

    def test_address_aliases_preserve_units_and_unsupported_patterns(self):
        result = normalize_addresses(
            [
                source(1, address="  1 Main St., Toronto  "),
                source(2, address="1 Main Street, Toronto"),
                source(3, address="1 Main St, Toronto Unit 2"),
                source(4, address="1 Main St, Toronto Unit 3"),
                source(5, address="東京都千代田区"),
            ],
            {},
        )
        matches = [
            x for x in result["items"] if x["label"] == "Candidate equivalent wording"
        ]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["source_ids"], [source(1).id, source(2).id])
        self.assertEqual(
            result["items"][0]["values"]["Original"], "  1 Main St., Toronto  "
        )
        self.assertEqual(
            result["items"][4]["values"]["Parsing scope"], "Unmatched address pattern"
        )

    def test_spatial_groups_do_not_turn_a_chain_into_one_place_or_visit(self):
        observations = [
            source(1, point=(0, 0), kind="location"),
            source(2, point=(0, 0.0008), kind="location"),
            source(3, point=(0, 0.0016), kind="location"),
            source(4, point=None),
        ]
        result = spatial_groups(observations, {"distance_m": 100})
        self.assertEqual(result["metrics"]["Spatial groups"], 2)
        self.assertEqual(result["metrics"]["Unlinked observations"], 3)
        self.assertTrue(any("no usable" in x for x in result["warnings"]))
        self.assertEqual(len(result["items"][0]["anchors"]), 2)

    def test_quality_separates_times_and_detects_original_row_order(self):
        batch = SimpleNamespace(timezone="America/Toronto")
        first = source(
            1,
            minute=10,
            import_batch_id=uuid4(),
            source_row=2,
            import_batch=batch,
            received_at=START,
        )
        second = source(
            2,
            minute=5,
            import_batch_id=first.item.import_batch_id,
            source_row=3,
            import_batch=batch,
        )
        missing = source(3, minute=None, point=None)
        result = location_quality([first, second, missing], {})
        self.assertIn("Receipt precedes event time", result["items"][0]["text"])
        self.assertEqual(
            result["items"][0]["values"]["Export timezone"], "America/Toronto"
        )
        self.assertEqual(
            result["items"][0]["values"]["Collection time"], "Not recorded"
        )
        self.assertTrue(
            any(
                x["label"] == "Source-row ordering discrepancy" for x in result["items"]
            )
        )
        self.assertIn("Event time is missing", result["items"][2]["text"])

    def test_travel_chronology_uses_original_subject_and_export_boundaries(self):
        batch = uuid4()
        observations = [
            source(1, minute=10, point=None, import_batch_id=batch, source_row=1),
            source(2, minute=5, point=None, import_batch_id=batch, source_row=2),
            source(3, minute=0, point=None, import_batch_id=uuid4(), source_row=3),
            source(
                4,
                minute=0,
                point=None,
                import_batch_id=batch,
                source_row=4,
                person_id=uuid4(),
            ),
        ]
        quality = location_quality(observations, {})
        travel = travel_discrepancies(observations, {})
        expected = [
            item
            for item in quality["items"]
            if item["label"] == "Source-row ordering discrepancy"
        ]
        self.assertEqual(len(expected), 1)
        self.assertEqual(travel["items"], expected)
        self.assertEqual(expected[0]["source_ids"], [s.id for s in observations[:2]])
        self.assertEqual(travel["outcome"], "insufficient")

    def test_reported_position_and_reviewed_address_position_remain_competing(self):
        original = source(1, point=(0, 0))
        interpreted = Source(
            original.id, original.kind, original.item, derived_coordinates=(1, 1)
        )
        result = location_quality([interpreted], {})
        self.assertIn("differ by", result["items"][0]["text"])
        self.assertEqual(interpreted.coordinates, (0.0, 0.0))

    def test_selected_distance_controls_quality_and_equal_time_discrepancies(self):
        original = source(1, point=(0, 0))
        interpreted = Source(
            original.id, original.kind, original.item, derived_coordinates=(0, 0.001)
        )
        second = source(2, point=(0, 0.001))
        for threshold, expected in ((100, True), (250, False)):
            with self.subTest(threshold=threshold):
                parameters = {"distance_m": threshold}
                quality = location_quality([interpreted], parameters)
                self.assertEqual("differ by" in quality["items"][0]["text"], expected)
                travel = travel_discrepancies([original, second], parameters)
                self.assertEqual(bool(travel["items"]), expected)

    def test_radius_and_polygon_show_boundary_ambiguity_and_missing_positions(self):
        result = proximity(
            [
                source(1, point=(0, 0)),
                source(2, point=(0, 0.01)),
                source(3, point=None),
            ],
            {
                "reference_latitude": 0,
                "reference_longitude": 0,
                "radius_m": distance_m((0, 0), (0, 0.01)),
            },
        )
        self.assertEqual(result["items"][0]["label"], "Inside candidate radius")
        self.assertEqual(result["items"][1]["label"], "Near boundary (uncertain)")
        self.assertEqual(result["metrics"]["Compared observations"], 2)
        result = proximity(
            [source(1, point=(0, 0)), source(2, point=(1, 1))],
            {
                "polygon": [[-1, -1], [-1, 1], [1, 1], [1, -1]],
            },
        )
        self.assertEqual(result["items"][0]["label"], "Inside candidate area")
        self.assertEqual(result["items"][1]["label"], "Near boundary (uncertain)")
        with self.assertRaises(ValueError):
            proximity([source(1)], {"reference_source_id": str(uuid4())})
        with self.assertRaises(ValueError):
            proximity([source(1)], {"polygon": [[0, 179], [0, -179], [1, 179]]})

    def test_degenerate_self_intersecting_and_duplicate_geofences_are_rejected(self):
        for vertices in (
            [[0, 0], [0, 1], [0, 2]],
            [[0, 0], [2, 2], [0, 2], [2, 0]],
            [[0, 0], [1, 1], [0, 0], [0, 1]],
        ):
            with self.subTest(vertices=vertices), self.assertRaises(ValueError):
                proximity([source(1)], {"polygon": vertices})

    def test_grouping_is_stable_across_new_run_source_identifiers(self):
        originals = [
            source(1, point=(0, 0)),
            source(2, point=(0, 0.0008)),
            source(3, point=(0, 0.0016)),
        ]
        rerun = [
            Source(str(uuid4()), value.kind, value.item)
            for value in reversed(originals)
        ]
        first = spatial_groups(originals, {})
        second = spatial_groups(rerun, {})
        self.assertEqual(
            [x["values"] for x in first["items"]],
            [x["values"] for x in second["items"]],
        )

    def test_dense_colocation_is_explicitly_partial_and_bounded(self):
        observations = [source(index, person_id=uuid4()) for index in range(50)]
        result = colocation(observations, {})
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(len(result["items"]), 1_000)
        self.assertTrue(any("capped" in value for value in result["warnings"]))

    def test_visits_count_revisits_not_samples_and_split_long_gaps(self):
        observations = [
            source(1),
            source(2, minute=5),
            source(3, minute=10),
            source(4, minute=15, point=(43.66, -79.38)),
            source(5, minute=20),
            source(6, minute=25),
            source(7, minute=100),
            source(8, minute=105),
        ]
        result = candidate_visits(observations, {})
        self.assertEqual(result["metrics"]["Candidate visits"], 3)
        counts = [x for x in result["items"] if x["label"].endswith("visit frequency")]
        self.assertEqual(counts[0]["values"]["Candidate visits"], 3)
        self.assertEqual(counts[0]["values"]["Observed samples"], 7)
        self.assertEqual(result["items"][0]["values"]["Observed span (seconds)"], 600)

    def test_unlinked_missing_time_and_stationary_data_do_not_become_a_journey(self):
        unlinked = [
            source(1, kind="location"),
            source(2, kind="location", minute=10, point=(44, -79)),
        ]
        self.assertEqual(candidate_trips(unlinked, {})["outcome"], "insufficient")
        self.assertEqual(
            candidate_visits(unlinked, {})["metrics"]["Candidate visits"], 0
        )
        stationary = [source(1), source(2, minute=10)]
        self.assertEqual(
            candidate_trips(stationary, {})["metrics"]["Candidate journeys"], 0
        )
        self.assertEqual(
            candidate_trips([source(1, minute=None)], {})["outcome"], "insufficient"
        )

    def test_journey_has_all_included_sources_and_never_bridges_large_gaps(self):
        observations = [
            source(1),
            source(2, minute=10),
            source(3, minute=20, point=(43.66, -79.38)),
            source(4, minute=30, point=(43.67, -79.38)),
            source(5, minute=40, point=(43.67, -79.38)),
            source(6, minute=200, point=(44, -79)),
        ]
        result = candidate_trips(list(reversed(observations)), {})
        self.assertEqual(result["metrics"]["Candidate journeys"], 1)
        self.assertEqual(
            result["items"][0]["source_ids"], [x.id for x in observations[1:4]]
        )
        self.assertEqual(
            result["items"][0]["values"]["Largest sampling interval (seconds)"], 600
        )

    def test_routines_keep_direction_and_expose_sampling_selection(self):
        observations = [
            source(1),
            source(2, minute=10, point=(43.66, -79.38)),
            source(3, minute=20, point=(43.66, -79.38)),
            source(4, minute=30),
            source(5, minute=40),
        ]
        result = movement_routines(observations, {})
        self.assertEqual(result["metrics"]["Directed place pairs"], 2)
        self.assertTrue(any("reporting density" in x for x in result["warnings"]))

    def test_visit_and_journey_gaps_link_actual_excluded_interval_endpoints(self):
        observations = [
            source(1),
            source(2, minute=5),
            source(3, minute=200),
            source(4, minute=205),
        ]
        for operation in (candidate_visits, candidate_trips):
            with self.subTest(operation=operation.__name__):
                result = operation(observations, {})
                gaps = [
                    item for item in result["items"] if item["label"] == "Coverage gap"
                ]
                self.assertEqual(len(gaps), 1)
                self.assertEqual(
                    gaps[0]["source_ids"], [observations[1].id, observations[2].id]
                )
                self.assertEqual(gaps[0]["values"]["Gap seconds"], 195 * 60)
                self.assertEqual(
                    gaps[0]["values"]["Maximum supported gap seconds"], 60 * 60
                )
                self.assertEqual(result["metrics"]["Excluded sampling gaps"], 1)
                self.assertIn("excluded", gaps[0]["text"])

    def test_alternative_versions_of_one_observation_cannot_inflate_counts(self):
        original = source(1)
        alternate = Source(
            str(uuid4()),
            original.kind,
            original.item,
            derived_coordinates=(43.7, -79.3),
        )
        for operation in (
            candidate_visits,
            candidate_trips,
            movement_routines,
            spatial_groups,
            colocation,
            travel_discrepancies,
        ):
            with (
                self.subTest(operation=operation.__name__),
                self.assertRaisesMessage(ValueError, "one interpretation"),
            ):
                operation([original, alternate], {})
        quality = location_quality([original, alternate], {})
        self.assertEqual(quality["metrics"]["Selected observations"], 1)
        self.assertEqual(quality["metrics"]["Selected interpretations"], 2)
        self.assertTrue(
            any(
                item["label"] == "Alternative interpretations of one observation"
                for item in quality["items"]
            )
        )
        self.assertFalse(
            any(
                item["label"] == "Repeated-observation candidate"
                for item in quality["items"]
            )
        )

    def comparison_parameters(self):
        return {
            "period_a_start": (START - timedelta(minutes=1)).isoformat(),
            "period_a_end": (START + timedelta(days=1)).isoformat(),
            "period_b_start": (START + timedelta(days=1)).isoformat(),
            "period_b_end": (START + timedelta(days=2)).isoformat(),
        }

    def repeated_journeys(self, *, alternative_corridor=False):
        origin, middle, destination = (0, 0), (0.01, 0), (0.02, 0)
        second_middle = (0.01, 0.01) if alternative_corridor else middle
        return [
            source(1, point=origin),
            source(2, minute=10, point=middle),
            source(3, minute=20, point=destination),
            source(4, minute=30, point=destination),
            source(5, minute=40, point=origin),
            source(6, minute=50, point=origin),
            source(7, minute=1440, point=origin),
            source(8, minute=1450, point=second_middle),
            source(9, minute=1460, point=destination),
            source(10, minute=1470, point=destination),
        ]

    def test_period_comparison_exposes_coverage_denominators_and_share_changes(self):
        observations = self.repeated_journeys() + [
            source(11, minute=55, point=None),
            source(12, minute=None),
        ]
        result = movement_routines(observations, self.comparison_parameters())
        coverage = {
            item["label"]: item["values"]
            for item in result["items"]
            if "observation coverage" in item["label"]
        }
        self.assertEqual(
            coverage["Period A observation coverage"]["Selected timed observations"], 7
        )
        self.assertEqual(
            coverage["Period A observation coverage"]["Usable timed positions"], 6
        )
        self.assertEqual(
            coverage["Period B observation coverage"]["Selected timed observations"], 4
        )
        self.assertEqual(
            coverage["Period B observation coverage"][
                "Window duration denominator (seconds)"
            ],
            86400,
        )
        self.assertEqual(
            coverage["Period A observation coverage"][
                "Timed observations without coordinates"
            ],
            1,
        )
        comparisons = [
            item for item in result["items"] if "period comparison" in item["label"]
        ]
        increased = next(
            item
            for item in comparisons
            if item["values"]["Period B pair journeys"] == 1
        )
        self.assertEqual(increased["values"]["Period A subject journey denominator"], 2)
        self.assertEqual(increased["values"]["Period B subject journey denominator"], 1)
        self.assertEqual(increased["values"]["Period A pair share"], 0.5)
        self.assertEqual(increased["values"]["Period B pair share"], 1)
        self.assertIn(
            "larger share", increased["values"]["Observed recurrence comparison"]
        )
        self.assertEqual(increased["values"]["Shared observed sequences"], 1)
        self.assertEqual(increased["values"]["Sequences first observed in B"], 0)
        self.assertTrue(
            any("no usable event time" in value for value in result["warnings"])
        )

    def test_observed_corridor_changes_preserve_endpoint_and_sampling_distinctions(
        self,
    ):
        result = movement_routines(
            self.repeated_journeys(alternative_corridor=True),
            self.comparison_parameters(),
        )
        compared = next(
            item
            for item in result["items"]
            if "period comparison" in item["label"]
            and item["values"]["Period B pair journeys"] == 1
        )
        self.assertEqual(compared["values"]["Shared observed sequences"], 0)
        self.assertEqual(compared["values"]["Sequences first observed in B"], 1)
        self.assertIn(
            "sampling density", compared["values"]["Observed corridor comparison"]
        )
        self.assertTrue(
            any(
                "not reconstructed road routes" in value for value in result["warnings"]
            )
        )

    def test_empty_comparison_period_is_not_an_asserted_behavior_change(self):
        result = movement_routines(
            self.repeated_journeys()[:6], self.comparison_parameters()
        )
        coverage = next(
            item
            for item in result["items"]
            if item["label"] == "Period B observation coverage"
        )
        self.assertEqual(coverage["values"]["Usable timed positions"], 0)
        self.assertEqual(
            coverage["values"]["Largest sampling interval (seconds)"], "Unknown"
        )
        for item in result["items"]:
            if "period comparison" in item["label"]:
                self.assertIn(
                    "Insufficient journey coverage",
                    item["values"]["Observed recurrence comparison"],
                )
                self.assertEqual(item["values"]["Period B pair share"], "Unknown")
                self.assertIn(
                    "Insufficient interior samples",
                    item["values"]["Observed corridor comparison"],
                )
                self.assertTrue(item["source_ids"])

    def test_period_validation_rejects_partial_naive_reversed_and_overlapping_windows(
        self,
    ):
        base = self.comparison_parameters()
        for changed in (
            {"period_b_end": None},
            {"period_a_start": "2026-10-01T09:59:00"},
            {"period_a_end": base["period_a_start"]},
            {"period_b_start": base["period_a_start"]},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                movement_routines(self.repeated_journeys(), base | changed)

    def test_monthly_summary_exposes_calendar_coverage_without_comparison_periods(self):
        result = movement_routines(self.repeated_journeys(), {})
        coverage = [
            item
            for item in result["items"]
            if item["label"] == "Selected calendar-month coverage"
        ]
        self.assertEqual(len(coverage), 1)
        self.assertEqual(
            coverage[0]["values"]["Window duration denominator (seconds)"], 31 * 86400
        )
        self.assertEqual(coverage[0]["values"]["Selected timed observations"], 10)
        self.assertFalse(result["metrics"]["Explicit comparison periods"])

    def test_colocation_tolerances_near_misses_and_subject_separation(self):
        observations = [
            source(1),
            source(2, minute=5, person_id=uuid4()),
            source(3, minute=30, person_id=uuid4()),
            source(4, minute=5, point=(44, -79), person_id=uuid4()),
            source(5, kind="location"),
        ]
        result = colocation(observations, {})
        self.assertEqual(result["metrics"]["Candidate pairs"], 1)
        self.assertIn(
            "no observed presence overlap",
            result["items"][0]["values"]["Temporal interpretation"],
        )
        self.assertEqual(
            colocation([source(1), source(2)], {})["metrics"]["Candidate pairs"], 0
        )

    def test_travel_checks_equal_time_fast_transitions_and_gaps(self):
        observations = [
            source(1, point=(0, 0)),
            source(2, point=(1, 0)),
            source(3, minute=1, point=(2, 0)),
            source(4, minute=200, point=(3, 0)),
        ]
        result = travel_discrepancies(observations, {})
        self.assertEqual(
            {x["label"] for x in result["items"]},
            {
                "Simultaneous position discrepancy",
                "Rapid-transition discrepancy",
                "Coverage gap",
            },
        )
        plausible = [source(1, point=(0, 0)), source(2, minute=60, point=(1, 0))]
        self.assertEqual(travel_discrepancies(plausible, {})["items"], [])

    def test_conflicts_require_explicit_same_event_selection(self):
        observations = [source(1), source(2, minute=10, point=(44, -79))]
        with self.assertRaises(ValueError):
            location_conflicts(observations, {})
        result = location_conflicts(observations, {"event_label": "Reported delivery"})
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(
            result["items"][1]["values"][
                "Time difference from first selected observation (seconds)"
            ],
            600,
        )
        self.assertIn("unresolved", result["items"][0]["text"])


GEOCODER_SETTINGS = {
    "PANDECT_GEOCODER_LOCAL": False,
    "PANDECT_GEOCODER_URL": "https://geocoder.example.test",
    "PANDECT_GEOCODER_NAME": "Synthetic reference",
    "PANDECT_GEOCODER_VERSION": "2026-10-01",
    "PANDECT_GEOCODER_ATTRIBUTION": "Synthetic test dataset",
    "PANDECT_GEOCODER_USER_AGENT": "Pandect test operator",
}


@override_settings(**GEOCODER_SETTINGS)
class GeocoderTests(SimpleTestCase):
    def test_http_error_bodies_close_including_reverse_lookup_no_match(self):
        for status, code in (
            (401, "geocoder_authentication_failed"),
            (429, "geocoder_rate_limited"),
            (500, "geocoder_unavailable"),
            (404, None),
        ):
            with self.subTest(status=status):
                body = io.BytesIO(b"Private upstream body")
                client = Geocoder()
                client.opener = Mock()
                client.opener.open.side_effect = HTTPError(
                    "https://geocoder.example.test/reverse", status, "failure", {}, body
                )
                if code is None:
                    self.assertEqual(client.lookup(coordinates=(0, 0)), [])
                else:
                    with self.assertRaisesMessage(GeocodingError, code):
                        client.lookup(coordinates=(0, 0))
                self.assertTrue(body.closed)

    def client_with_response(self, value):
        client = Geocoder()
        response = Mock()
        response.read.return_value = (
            value if isinstance(value, bytes) else json.dumps(value).encode()
        )
        client.opener = Mock()
        client.opener.open.return_value.__enter__ = Mock(return_value=response)
        client.opener.open.return_value.__exit__ = Mock(return_value=False)
        return client

    def test_lookup_is_bounded_and_retains_alternatives_and_granularity(self):
        client = self.client_with_response(
            [
                {
                    "lat": "43.65",
                    "lon": "-79.38",
                    "display_name": "Test building",
                    "addresstype": "building",
                    "boundingbox": ["43", "44", "-80", "-79"],
                    "osm_type": "way",
                    "osm_id": 123,
                },
                {
                    "lat": "43.66",
                    "lon": "-79.37",
                    "display_name": "Test street",
                    "type": "street",
                },
            ]
        )
        results = client.lookup(address="1 Main St")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["granularity"], "building")
        self.assertEqual(results[0]["reference_id"], "way:123")
        request = client.opener.open.call_args.args[0]
        self.assertIn("/search?", request.full_url)
        self.assertIn("q=1+Main+St", request.full_url)
        self.assertEqual(client.opener.open.call_args.kwargs["timeout"], 10)

    def test_can_country_component_becomes_filter_preserving_other_address_fields(self):
        for original, submitted in (
            (
                "12 Example St, Testville, Ontario, CAN, K1A 0B1",
                "12 Example St, Testville, Ontario, K1A 0B1",
            ),
            (
                " 12 Example St , Testville , Ontario , can , k1a0b1 ",
                " 12 Example St , Testville , Ontario , k1a0b1 ",
            ),
            (
                "Unit 4, 12 Example St, Testville, Ontario, CAN, K1A0B1",
                "Unit 4, 12 Example St, Testville, Ontario, K1A0B1",
            ),
        ):
            with self.subTest(original=original):
                client = self.client_with_response([])
                self.assertEqual(client.lookup(address=original), [])
                client.opener.open.assert_called_once()
                request = client.opener.open.call_args.args[0]
                query = parse_qs(urlsplit(request.full_url).query)
                self.assertEqual(query["q"], [submitted])
                self.assertEqual(query["countrycodes"], ["ca"])
                self.assertEqual(query["limit"], ["5"])

    def test_other_address_formats_are_submitted_without_reinterpretation(self):
        for address in (
            "12 CAN St, Testville, Ontario, Canada, K1A 0B1",
            "12 Example St, Testville, Ontario, Canada, K1A 0B1",
            "12 Example St, Testville, Ontario, USA, K1A 0B1",
            "12 Example St, Testville, Ontario, CAN, unknown",
            "12 Example St, Testville, CAN, K1A 0B1",
            "12 Example St, , Ontario, CAN, K1A 0B1",
            "CAN, 12 Example St, Testville, Ontario, K1A 0B1",
        ):
            with self.subTest(address=address):
                client = self.client_with_response([])
                client.lookup(address=address)
                request = client.opener.open.call_args.args[0]
                query = parse_qs(urlsplit(request.full_url).query)
                self.assertEqual(query["q"], [address])
                self.assertNotIn("countrycodes", query)
                client.opener.open.assert_called_once()

    def test_adjusted_query_and_original_are_retained_for_every_lookup_outcome(self):
        original = "12 Example St, Testville, Ontario, CAN, K1A 0B1"
        submitted = "12 Example St, Testville, Ontario, K1A 0B1"
        candidate = {
            "lat": "43.65",
            "lon": "-79.38",
            "display_name": "12 Example Street, Testville, Canada",
            "addresstype": "building",
        }
        for outcome in ("candidate", "no_match", "unavailable"):
            with self.subTest(outcome=outcome):
                observation = source(1, address=original, sha256="retained-checksum")
                client = self.client_with_response(
                    [candidate] if outcome == "candidate" else []
                )
                if outcome == "unavailable":
                    client.opener.open.side_effect = HTTPError(
                        "https://geocoder.example.test/search",
                        503,
                        "failure",
                        {},
                        io.BytesIO(b"Private upstream body"),
                    )
                result = geocode_locations(
                    [observation],
                    {"external_processing": True},
                    client=client,
                    authorize=Mock(),
                )
                values = result["items"][0]["values"]
                self.assertEqual(values["Lookup direction"], "search")
                self.assertEqual(values["Lookup query"], submitted)
                self.assertEqual(values["Original address"], original)
                self.assertEqual(values["Country filter"], "ca")
                self.assertTrue(values["Query adjustment"])
                self.assertEqual(observation.item.address, original)
                self.assertEqual(observation.item.sha256, "retained-checksum")
                client.opener.open.assert_called_once()
                if outcome == "candidate":
                    self.assertEqual(values["Review"], "Unresolved")
                elif outcome == "unavailable":
                    self.assertEqual(values["Failure code"], "geocoder_unavailable")
                    self.assertEqual(result["outcome"], "partial")
                else:
                    self.assertEqual(result["items"][0]["label"], "No geocode match")

    def test_reverse_lookup_does_not_adapt_the_original_address(self):
        client = self.client_with_response(
            {"lat": "43.65", "lon": "-79.38", "display_name": "Synthetic place"}
        )
        observation = source(
            1, address="12 Example St, Testville, Ontario, CAN, K1A 0B1"
        )
        result = geocode_locations(
            [observation],
            {"external_processing": True, "reverse": True},
            client=client,
            authorize=Mock(),
        )
        request = client.opener.open.call_args.args[0]
        query = parse_qs(urlsplit(request.full_url).query)
        self.assertEqual(urlsplit(request.full_url).path, "/reverse")
        self.assertEqual(query["lat"], ["43.65"])
        self.assertEqual(query["lon"], ["-79.38"])
        self.assertNotIn("q", query)
        self.assertNotIn("countrycodes", query)
        values = result["items"][0]["values"]
        self.assertEqual(values["Lookup direction"], "reverse")
        self.assertEqual(values["Lookup query"], str(observation.coordinates))
        self.assertNotIn("Original address", values)
        self.assertNotIn("Country filter", values)

    def test_adjustment_keeps_distinct_original_queries_and_repeated_source_links(self):
        original = "12 Example St, Testville, Ontario, CAN, K1A 0B1"
        variant = original.replace(", CAN,", ", can,")
        client = self.client_with_response([])
        observations = [
            source(1, address=original),
            source(2, address=original),
            source(3, address=variant),
        ]
        result = geocode_locations(
            observations,
            {"external_processing": True},
            client=client,
            authorize=Mock(),
        )
        self.assertEqual(result["metrics"]["Unique lookups"], 2)
        self.assertEqual(client.opener.open.call_count, 2)
        self.assertEqual(
            [item["source_ids"] for item in result["items"]],
            [[observations[0].id, observations[1].id], [observations[2].id]],
        )
        self.assertEqual(
            [item["values"]["Original address"] for item in result["items"]],
            [original, variant],
        )
        self.assertEqual(
            result["items"][0]["values"]["Lookup query"],
            result["items"][1]["values"]["Lookup query"],
        )

    def test_no_public_default_and_no_unconfigured_lookup(self):
        with override_settings(PANDECT_GEOCODER_URL=""):
            with self.assertRaisesMessage(GeocodingError, "geocoder_not_configured"):
                Geocoder()
        with override_settings(
            PANDECT_GEOCODER_URL="https://nominatim.openstreetmap.org"
        ):
            with self.assertRaisesMessage(
                GeocodingError, "public_geocoder_not_supported"
            ):
                Geocoder()

    def test_invalid_and_oversized_responses_do_not_publish_candidates(self):
        for value in (
            [{"lat": "nan", "lon": 0}],
            [{"lat": 91, "lon": 0}],
            b"not json",
            b"x" * (MAX_RESPONSE_BYTES + 1),
        ):
            with (
                self.subTest(value_type=type(value)),
                self.assertRaises(GeocodingError),
            ):
                self.client_with_response(value).lookup(address="Synthetic place")

    def test_batches_require_consent_and_fresh_authorization_and_cache_repeated_queries(
        self,
    ):
        client = Mock(name="geocoder")
        client.name = "Synthetic reference"
        client.version = "v1"
        client.attribution = "Synthetic dataset"
        client.lookup.return_value = [
            {
                "latitude": 43,
                "longitude": -79,
                "address": "Candidate",
                "granularity": "street",
            }
        ]
        authorize = Mock()
        with self.assertRaisesMessage(
            GeocodingError, "external_processing_confirmation_required"
        ):
            geocode_locations([source(1)], {}, client=client, authorize=authorize)
        with self.assertRaisesMessage(GeocodingError, "current_authorization_required"):
            geocode_locations([source(1)], {"external_processing": True}, client=client)
        result = geocode_locations(
            [source(1), source(2)],
            {"external_processing": True},
            client=client,
            authorize=authorize,
        )
        self.assertEqual(result["metrics"]["Unique lookups"], 1)
        self.assertEqual(client.lookup.call_count, 1)
        self.assertEqual(authorize.call_count, 2)
        self.assertEqual(len(result["items"][0]["source_ids"]), 2)
        self.assertEqual(result["items"][0]["values"]["Review"], "Unresolved")

    def test_partial_failure_keeps_completed_lookups_and_safe_error(self):
        client = Mock()
        client.name, client.version, client.attribution = "Synthetic", "v1", "Synthetic"
        client.lookup.side_effect = [[], GeocodingError("geocoder_rate_limited")]
        result = geocode_locations(
            [source(1), source(2, address="2 Main St")],
            {"external_processing": True},
            client=client,
            authorize=Mock(),
        )
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(result["items"][0]["label"], "No geocode match")
        self.assertEqual(
            result["items"][1]["values"]["Failure code"], "geocoder_rate_limited"
        )

    @override_settings(PANDECT_GEOCODER_URL="")
    def test_all_ineligible_observations_do_not_need_a_configured_lookup(self):
        authorize = Mock()
        result = geocode_locations(
            [source(1, address="")],
            {"external_processing": True},
            authorize=authorize,
        )
        self.assertEqual(result["outcome"], "insufficient")
        self.assertEqual(result["metrics"]["Unsupported observations"], 1)
        self.assertEqual(result["metrics"]["Unique lookups"], 0)
        self.assertEqual(result["items"][0]["label"], "Lookup unsupported")
        authorize.assert_not_called()

    def test_revocation_prevents_next_external_lookup(self):
        client = Mock()
        authorize = Mock(side_effect=PermissionError("Access removed"))
        with self.assertRaises(PermissionError):
            geocode_locations(
                [source(1)],
                {"external_processing": True},
                client=client,
                authorize=authorize,
            )
        client.lookup.assert_not_called()
