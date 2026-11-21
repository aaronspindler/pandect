"""Saved trip invalidation at the evidence/import/deletion boundaries."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from core.evidence.deletion import execute_deletion, plan_deletion
from core.testing import EvidenceViewTestMixin
from geo.models import (
    VehicleLocationEvidence,
    VehicleTripGeneration,
    VehicleTripInput,
    VehicleTripState,
)
from geo.trip_jobs import process_trips
from geo.trip_processing import prepare_case_trips, saved_trips


class TripEvidenceIntegrationTests(EvidenceViewTestMixin, TestCase):
    def source(self, minute, address, **values):
        return VehicleLocationEvidence.objects.create(
            **(
                {
                    "case": self.case,
                    "vehicle": self.vehicle,
                    "uploaded_by": self.users["owner"],
                    "title": "Retained vehicle observation",
                    "address": address,
                    "observed_at": datetime(2026, 11, 1, tzinfo=UTC)
                    + timedelta(minutes=minute),
                }
                | values
            )
        )

    def build(self):
        generations = prepare_case_trips(self.users["owner"], self.case.pk)
        for generation in generations:
            for _ in range(10):
                generation.refresh_from_db()
                if generation.status != "queued":
                    break
                process_trips(str(generation.pk), str(generation.token))
            generation.refresh_from_db()
            self.assertEqual(generation.status, "complete", generation.error_code)
        return generations[0]

    def test_add_archive_restore_and_duplicate_submission_queue_atomically(self):
        self.source(0, "10 Example Street")
        self.source(5, "20 Example Street")
        self.build()
        self.login("editor")
        payload = self.payload(
            "vehicle-location",
            address="30 Example Street",
            observed_at="2026-11-01T00:10:00Z",
        )
        response = self.client.post(
            self.url("create"), payload, HTTP_ACCEPT="application/json"
        )
        self.assertEqual(response.status_code, 201, response.content)
        source = VehicleLocationEvidence.objects.get(pk=response.json()["id"])
        state = VehicleTripState.objects.get(vehicle=self.vehicle)
        self.assertIsNone(state.current_generation_id)
        self.assertFalse(saved_trips(self.case).exists())
        count = VehicleTripGeneration.objects.count()
        retry = self.client.post(
            self.url("create"), payload, HTTP_ACCEPT="application/json"
        )
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(VehicleTripGeneration.objects.count(), count)
        self.build()
        for action in ("archive", "restore"):
            response = self.client.post(self.url(action, source, "vehicle-location"))
            self.assertEqual(response.status_code, 302)
            self.assertFalse(saved_trips(self.case).exists())
            self.build()
        with patch("geo.tasks.process_vehicle_trips") as task:
            task.enqueue.side_effect = RuntimeError
            with self.assertRaises(RuntimeError):
                self.client.post(self.url("archive", source, "vehicle-location"))
        source.refresh_from_db()
        self.assertFalse(source.is_archived)
        self.assertTrue(saved_trips(self.case).exists())

    def test_deletion_inventory_includes_generation_even_for_nontrip_input(self):
        self.source(0, "10 Example Street")
        self.source(5, "20 Example Street")
        untimed = self.source(10, "30 Example Street", observed_at=None)
        generation = self.build()
        self.assertFalse(
            generation.trips.filter(sources__input__observation=untimed).exists()
        )
        plan = plan_deletion(untimed, "vehicle-location")
        self.assertFalse(plan.blocker)
        self.assertIn(str(generation.pk), {entry["id"] for entry in plan.entries})
        actor = self.users["viewer"]
        actor.is_superuser = True
        actor.save(update_fields=("is_superuser",))
        execute_deletion(plan, actor)
        self.assertFalse(
            VehicleTripGeneration.objects.filter(pk=generation.pk).exists()
        )
        self.assertFalse(VehicleTripInput.objects.exists())
        self.assertFalse(saved_trips(self.case).exists())
        self.build()
        self.assertTrue(saved_trips(self.case).exists())

    def test_processing_history_and_whole_case_deletion(self):
        self.source(0, "10 Example Street")
        self.source(5, "20 Example Street")
        generation = self.build()
        for geocode_status in ("queued", "complete"):
            VehicleTripGeneration.objects.filter(pk=generation.pk).update(
                geocode_status=geocode_status
            )
            for role in ("owner", "editor", "viewer"):
                self.login(role)
                response = self.client.get(
                    reverse("core:analysis:processing", args=(self.case.pk,))
                )
                self.assertContains(response, str(self.vehicle))
                self.assertContains(
                    response,
                    "Trip processing history",
                    count=int(geocode_status == "complete"),
                )
                self.assertContains(
                    response,
                    "Preparing local address lookups",
                    count=int(geocode_status == "queued"),
                )
        self.case.delete()
        self.assertFalse(
            VehicleTripGeneration.objects.filter(pk=generation.pk).exists()
        )
        self.assertFalse(VehicleTripInput.objects.exists())
