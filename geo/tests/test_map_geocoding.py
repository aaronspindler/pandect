"""Trip paths use original coordinates or exactly one currently reviewed estimate."""

from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from core.analysis.services import process_run, start_run
from core.models import AnalysisReview, AnalysisRun
from geo.analysis.external.geocoding import Geocoder
from geo.testing import LOCAL_GEOCODER
from geo.tests.test_map import SavedTripFixtures
from geo.trip_geometry import geometry_revision, source_positions
from geo.trip_processing import trip_sources


@override_settings(**LOCAL_GEOCODER)
class ReviewedTripGeometryTests(SavedTripFixtures, TestCase):
    def setUp(self):
        self.address_trip, self.addresses = self.add_trip(addresses=True, minute=60)
        self.address_url = reverse(
            "geo:trip_detail", args=(self.case.pk, self.address_trip.pk)
        )
        self.client.force_login(self.viewer)

    def lookup(self, candidates=None, *, complete=True):
        run, _ = start_run(
            self.owner,
            self.case.pk,
            "geocode_locations",
            [{"kind": "vehicle-location", "id": str(self.addresses[0].pk)}],
            parameters={"reverse": False},
        )
        if complete:
            with patch.object(
                Geocoder,
                "lookup",
                return_value=candidates
                or [
                    {
                        "latitude": 43.65,
                        "longitude": -79.39,
                        "address": "Synthetic resolved road",
                        "granularity": "street",
                        "reference_id": "way:1",
                        "licence": "Synthetic licence",
                    }
                ],
            ):
                process_run(str(run.pk))
            run.refresh_from_db()
            self.assertEqual(run.status, "complete")
        return run

    def review(self, run, index=0, decision="accepted"):
        return AnalysisReview.objects.create(
            case=self.case,
            result=run.result,
            reviewed_by=self.owner,
            item_index=index,
            decision=decision,
        )

    def positions(self):
        return self.client.get(self.address_url, HTTP_ACCEPT="application/json").json()

    def test_address_sources_require_review_before_mapping(
        self,
    ):
        original = (
            self.addresses[0].address,
            self.addresses[0].latitude,
            self.addresses[0].sha256,
        )
        run = self.lookup()
        data = self.positions()
        self.assertEqual(len(data["positions"]), 3)
        self.assertTrue(all(row["position"] is None for row in data["positions"]))
        detail = self.client.get(self.address_url)
        self.assertContains(detail, "Review address lookup")
        self.assertContains(detail, self.addresses[0].address)
        self.review(run)
        accepted = self.positions()
        self.assertEqual(accepted["positions"][0]["position"], [43.65, -79.39])
        self.assertIsNone(accepted["positions"][1]["position"])
        self.assertEqual(accepted["positions"][0]["label"], "Reviewed address estimate")
        self.assertIn("Synthetic licence", accepted["positions"][0]["attribution"])
        self.assertNotEqual(data["revision"], accepted["revision"])
        self.addresses[0].refresh_from_db()
        self.assertEqual(
            (
                self.addresses[0].address,
                self.addresses[0].latitude,
                self.addresses[0].sha256,
            ),
            original,
        )
        self.review(run, decision="rejected")
        self.assertIsNone(self.positions()["positions"][0]["position"])
        self.assertEqual(
            self.client.get(
                self.address_url,
                {"revision": accepted["revision"]},
                HTTP_ACCEPT="application/json",
            ).status_code,
            409,
        )

    def test_ambiguous_multiple_accepted_estimates_do_not_choose_a_route(self):
        run = self.lookup(
            [
                {
                    "latitude": 43.65,
                    "longitude": -79.39,
                    "address": "Synthetic first",
                    "reference_id": "way:1",
                },
                {
                    "latitude": 43.66,
                    "longitude": -79.40,
                    "address": "Synthetic second",
                    "reference_id": "way:2",
                },
            ]
        )
        self.review(run, 0)
        self.review(run, 1)
        data = self.positions()
        self.assertIsNone(data["positions"][0]["position"])
        self.assertIn("Multiple accepted", data["positions"][0]["label"])
        self.review(run, 0, "rejected")
        self.assertEqual(self.positions()["positions"][0]["position"], [43.66, -79.4])

    def test_pending_unpinned_or_external_results_do_not_supply_positions(
        self,
    ):
        run = self.lookup()
        self.review(run)
        before = geometry_revision(self.case, self.address_trip)
        self.lookup(complete=False)
        self.assertNotEqual(before, geometry_revision(self.case, self.address_trip))
        self.assertIsNone(self.positions()["positions"][0]["position"])
        # Excluding the newer pending attempt restores only a valid retained local run.
        AnalysisRun.objects.exclude(pk=run.pk).update(operation="location_quality")
        for values in (
            {"external_processing": True},
            {"method_version": "legacy"},
            {"parameters": {"reverse": False}},
        ):
            saved = {key: getattr(run, key) for key in values}
            AnalysisRun.objects.filter(pk=run.pk).update(**values)
            self.assertIsNone(self.positions()["positions"][0]["position"])
            AnalysisRun.objects.filter(pk=run.pk).update(**saved)

    def test_reported_and_polar_coordinates_preserve_originals(
        self,
    ):
        inputs = list(trip_sources(self.case, self.trip))
        with patch("geo.trip_geometry._latest_local_lookup") as lookups:
            positions = source_positions(self.case, inputs)
        lookups.assert_not_called()
        self.assertEqual(positions[0]["position"], [43.64, -79.38])
        inputs[0].snapshot = inputs[0].snapshot | {"latitude": "90"}
        rows = source_positions(self.case, inputs[:1])
        self.assertIsNone(rows[0]["position"])
        self.assertEqual(rows[0]["position_label"], "Outside map display range")

    def test_bad_cursor_and_changed_revision_are_recoverable(self):
        self.assertEqual(
            self.client.get(
                self.address_url, {"cursor": "-1"}, HTTP_ACCEPT="application/json"
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.get(
                self.address_url, {"cursor": "x" * 500}, HTTP_ACCEPT="application/json"
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.get(
                self.address_url,
                {"revision": "changed"},
                HTTP_ACCEPT="application/json",
            ).status_code,
            409,
        )

    def test_disabled_lookup_explains_unresolved_sources_but_keeps_retained_estimates(
        self,
    ):
        with override_settings(PANDECT_GEOCODER_LOCAL=False, PANDECT_GEOCODER_URL=""):
            self.assertEqual(
                self.positions()["positions"][0]["label"],
                "Local address lookup is not configured",
            )
        run = self.lookup()
        self.review(run)
        with override_settings(PANDECT_GEOCODER_LOCAL=False, PANDECT_GEOCODER_URL=""):
            positions = self.positions()["positions"]
        self.assertEqual(positions[0]["position"], [43.65, -79.39])
        self.assertEqual(
            positions[1]["label"], "Local address lookup is not configured"
        )
