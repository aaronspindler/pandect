"""Automatically visible trips from reported addresses and coordinates."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import Http404
from django.test import TestCase
from django.urls import reverse

from core.analysis.reports import report
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from geo.models import VehicleLocationEvidence, VehicleLocationImport, VehicleTrip
from geo.testing import csv_content, parse_csv
from geo.trip_jobs import process_trips
from geo.trip_processing import (
    format_trip,
    invalidate_vehicle_trips,
    prepare_case_trips,
)
from geo.vehicle_import_jobs import (
    enqueue_completion,
    enqueue_preparation,
    process_import,
)
from vehicle.models import Vehicle

INSTANT = datetime(2026, 10, 27, 12, tzinfo=UTC)


class VehicleTripTests(EvidenceViewTestMixin, TestCase):
    def observation(self, minute=0, address="10 Example Street", **values):
        observation = VehicleLocationEvidence.objects.create(
            **(
                {
                    "case": self.case,
                    "uploaded_by": self.users["owner"],
                    "title": "Synthetic vehicle observation",
                    "vehicle": self.vehicle,
                    "address": address,
                    "observed_at": INSTANT + timedelta(minutes=minute),
                }
                | values
            )
        )
        invalidate_vehicle_trips(
            self.users["owner"], self.case, [observation.vehicle_id]
        )
        return observation

    def trips(self, **values):
        self.prepare()
        result = report(self.case, "vehicle-trips", **values)
        # Inspect source membership explicitly; rendered summaries link to the
        # independently paginated provenance instead of embedding whole histories.
        result["items"] = [
            item
            | format_trip(
                self.case,
                VehicleTrip.objects.get(pk=item["id"]),
                include_sources=True,
            )
            for item in result["items"]
        ]
        return result

    def prepare(self):
        for generation in prepare_case_trips(self.users["owner"], self.case.pk):
            for _ in range(100):
                generation.refresh_from_db()
                if generation.status != "queued":
                    break
                process_trips(str(generation.pk), str(generation.token))
            generation.refresh_from_db()
            self.assertEqual(generation.status, "complete", generation.error_code)

    def test_address_trips_split_at_stops_and_retain_every_source(self):
        records = [
            self.observation(0),
            self.observation(5),
            self.observation(10, "20 Example Street"),
            self.observation(15, "30 Example Street"),
            self.observation(20, "30 Example Street"),
            self.observation(25, "40 Example Street"),
        ]
        result = self.trips()
        self.assertEqual(result["metrics"]["Possible trips"], 2)
        newer, older = result["items"]
        self.assertEqual(older["start"]["time"], records[1].observed_at)
        self.assertEqual(older["end"]["time"], records[3].observed_at)
        self.assertEqual(newer["start"]["time"], records[4].observed_at)
        self.assertEqual(newer["end"]["time"], records[5].observed_at)
        self.assertEqual(len(older["sources"]), 3)
        self.assertIn(str(records[2].pk), older["sources"][1]["url"])
        self.assertNotIn("distance", str(older["values"]).lower())

    def test_stationary_missing_times_and_different_vehicles_do_not_make_trips(self):
        self.observation(0, "10 Example St.")
        self.observation(10, "10 Example Street")
        self.observation(20, "20 Example Street", observed_at=None)
        another = Vehicle.objects.create(case=self.case, model="Other vehicle")
        self.observation(15, "30 Example Street", vehicle=another)
        result = self.trips()
        self.assertEqual(result["items"], [])
        self.assertEqual(result["metrics"]["Missing observation time"], 1)

    def test_gaps_conflicts_and_incomparable_positions_are_not_bridged(self):
        before = self.observation(0)
        after = self.observation(61, "20 Example Street")
        self.observation(61, "30 Example Street")
        self.observation(62, "", latitude=43, longitude=-79)
        result = self.trips()
        self.assertEqual(result["items"], [])
        self.assertEqual(result["metrics"]["Coverage gaps"], 1)
        self.assertIn(str(before.pk), result["gaps"][0]["start"]["url"])
        self.assertEqual(result["gaps"][0]["end"]["time"], after.observed_at)
        self.assertIn("same-time", " ".join(result["warnings"]))
        self.assertIn("cannot be compared", " ".join(result["warnings"]))

    def test_coordinates_take_priority_over_repeated_address_wording(self):
        self.observation(0, latitude=43, longitude=-79)
        self.observation(10, latitude="43.01", longitude=-79)
        result = self.trips()
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["basis"], "Reported coordinates")

    def test_positive_reported_speed_does_not_form_a_stop(self):
        self.observation(0, "10 Example Street")
        self.observation(5, "20 Example Street", speed_kmh=10)
        self.observation(10, "20 Example Street", speed_kmh=10)
        self.observation(15, "30 Example Street")
        result = self.trips()
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(len(result["items"][0]["sources"]), 4)

    def test_complete_history_filters_and_new_evidence_refresh(self):
        self.observation(0)
        self.observation(2, "20 Example Street")
        latest = self.observation(4, "30 Example Street")
        result = self.trips(page_size=2)
        self.assertFalse(result["metrics"]["Truncated"])
        self.assertEqual(result["metrics"]["Matching records"], 3)
        self.assertEqual(len(result["items"][0]["sources"]), 3)
        self.assertEqual(result["items"][0]["end"]["time"], latest.observed_at)
        self.assertEqual(len(self.trips(end=INSTANT)["items"]), 1)
        next_item = self.observation(6, "40 Example Street")
        self.assertEqual(self.trips()["items"][0]["end"]["time"], next_item.observed_at)
        VehicleLocationEvidence.objects.filter(pk=next_item.pk).update(
            is_archived=True,
            archive_changed_at=INSTANT,
            archive_changed_by=self.users["owner"],
        )
        invalidate_vehicle_trips(self.users["owner"], self.case, [self.vehicle.pk])
        self.assertEqual(self.trips()["items"][0]["end"]["time"], latest.observed_at)
        self.assertEqual(
            self.trips(state="all")["items"][0]["end"]["time"], next_item.observed_at
        )

    def test_cross_case_vehicles_and_corrupt_import_provenance_are_excluded(self):
        other = Vehicle.objects.create(case=self.other_case, model="Private vehicle")
        forged = self.observation(0)
        VehicleLocationEvidence.objects.filter(pk=forged.pk).update(vehicle=other)
        document = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Source",
            file=SimpleUploadedFile("source.csv", csv_content()),
        )
        batch = VehicleLocationImport.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            document=document,
            timezone="UTC",
        )
        self.observation(10, import_batch=batch, source_row=2)
        DocumentEvidence.objects.filter(pk=document.pk).update(case=self.other_case)
        result = report(self.case, "vehicle-trips")
        self.assertEqual(result["metrics"]["Matching records"], 0)
        with self.assertRaises(Http404):
            report(self.case, "vehicle-trips", vehicle_id=other.pk)

    def test_completed_address_only_csv_immediately_exposes_trips_without_analysis(
        self,
    ):
        first = parse_csv(csv_content(), "UTC")[0].raw
        content = csv_content(
            first,
            first
            | {
                "Address": "20 Example Street",
                "Created Time": "2026-09-16, 3:07:22 PM",
            },
        )
        document = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic trip CSV",
            file=SimpleUploadedFile("trips.csv", content),
        )
        batch = VehicleLocationImport.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            document=document,
            timezone="UTC",
        )
        enqueue_preparation(self.users["owner"], self.case.pk, batch.pk)
        batch.refresh_from_db()
        self.assertEqual(process_import(batch.pk, batch.job_token), "review")
        enqueue_completion(
            self.users["owner"], self.case.pk, batch.pk, {"0": str(self.vehicle.pk)}
        )
        batch.refresh_from_db()
        self.assertEqual(process_import(batch.pk, batch.job_token), "complete")
        with patch("geo.analysis.external.geocoding.Geocoder.lookup") as geocoder:
            result = self.trips()
        geocoder.assert_not_called()
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual([s["row"] for s in result["items"][0]["sources"]], [2, 3])
        self.assertIn(str(batch.pk), result["items"][0]["start"]["import_url"])
        self.assertTrue(all(item.latitude is None for item in batch.observations.all()))

    def test_report_and_vehicle_pages_allow_current_case_roles_and_deny_revocation(
        self,
    ):
        self.observation(0)
        self.observation(10, "20 Example Street")
        self.prepare()
        urls = [
            reverse(
                "vehicle:detail",
                kwargs={"case_id": self.case.pk, "vehicle_id": self.vehicle.pk},
            ),
            reverse(
                "core:analysis:report",
                kwargs={"case_id": self.case.pk, "name": "vehicle-trips"},
            ),
        ]
        for role in ("owner", "editor", "viewer"):
            self.login(role)
            for url in urls:
                response = self.client.get(url)
                self.assertContains(response, "Possible trip")
                self.assertContains(response, "10 Example Street")
                self.assertContains(response, "20 Example Street")
        self.case.memberships.filter(user=self.users["viewer"]).delete()
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 404)
        self.login("outsider")
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 404)
