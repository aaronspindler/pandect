"""Saved full-history trips, resumable publication and current authorization."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.test import TestCase, override_settings

from core.analysis.services import process_run
from core.models import AnalysisRun
from core.testing import EvidenceViewTestMixin
from geo.models import VehicleLocationEvidence
from geo.testing import LOCAL_GEOCODER
from geo.trip_jobs import prepare_addresses, process_trips
from geo.trip_models import VehicleTripGeneration, VehicleTripInput, VehicleTripState
from geo.trip_processing import (
    current_generations,
    format_trip,
    invalidate_vehicle_trips,
    prepare_case_trips,
    saved_trips,
    trip_sources,
)
from vehicle.models import Vehicle

INSTANT = datetime(2026, 11, 15, 12, tzinfo=UTC)


@override_settings(PANDECT_GEOCODER_LOCAL=False)
class SavedTripProcessingTests(EvidenceViewTestMixin, TestCase):
    def observations(self, count, *, stationary=None, positive=None):
        rows = []
        for index in range(count):
            item = VehicleLocationEvidence(
                case=self.case,
                uploaded_by=self.users["owner"],
                vehicle=self.vehicle,
                title=f"Synthetic sample {index}",
                address="100 Stop Street"
                if stationary and index in stationary
                else f"{index + 1} Moving Street",
                observed_at=INSTANT + timedelta(minutes=index),
                speed_kmh=10 if positive and index in positive else None,
            )
            item._prepare_original()
            rows.append(item)
        VehicleLocationEvidence.objects.bulk_create(rows)
        return rows

    def prepare(self, actor="owner"):
        return prepare_case_trips(self.users[actor], self.case.pk)[0]

    def drain(self, generation):
        for _ in range(30):
            generation.refresh_from_db()
            if generation.status != "queued":
                break
            process_trips(str(generation.pk), str(generation.token))
        generation.refresh_from_db()
        self.assertEqual(generation.status, "complete", generation.error_code)
        return generation

    def test_complete_long_trip_exceeds_old_caps_and_publishes_only_after_all_steps(
        self,
    ):
        rows = self.observations(1102)
        generation = self.prepare()
        first_token = str(generation.token)
        self.assertEqual(process_trips(str(generation.pk), first_token), "queued")
        generation.refresh_from_db()
        self.assertEqual(generation.processed_sources, 500)
        self.assertFalse(saved_trips(self.case).exists())
        self.assertEqual(process_trips(str(generation.pk), first_token), "unavailable")
        self.assertEqual(
            VehicleTripInput.objects.filter(generation=generation).count(), 500
        )
        self.drain(generation)
        trip = saved_trips(self.case).get()
        self.assertEqual(trip.source_count, 1102)
        self.assertEqual(trip.start_at, rows[0].observed_at)
        self.assertEqual(trip.end_at, rows[-1].observed_at)
        self.assertEqual(trip_sources(self.case, trip).count(), 1102)
        self.assertEqual(
            format_trip(self.case, trip, include_sources=True)["source_count"], 1102
        )
        self.assertEqual(generation.metrics["active"]["observations"], 1102)
        self.assertEqual(len(generation.carry), 0)

    def test_stop_crossing_chunk_boundary_splits_and_late_speed_does_not(self):
        self.observations(1002, stationary=range(498, 506))
        self.drain(self.prepare())
        trips = list(saved_trips(self.case).order_by("start_at"))
        self.assertEqual(len(trips), 2)
        self.assertEqual(trips[0].end_at, INSTANT + timedelta(minutes=498))
        self.assertEqual(trips[1].start_at, INSTANT + timedelta(minutes=505))
        VehicleLocationEvidence.objects.filter(
            vehicle=self.vehicle, observed_at=INSTANT + timedelta(minutes=505)
        ).update(speed_kmh=10)
        self.drain(
            invalidate_vehicle_trips(self.users["owner"], self.case, [self.vehicle.pk])[
                0
            ]
        )
        trip = saved_trips(self.case).get()
        self.assertEqual(trip.source_count, 1002)

    def test_archive_variants_are_independent_and_time_filters_keep_whole_trips(self):
        rows = self.observations(5)
        VehicleLocationEvidence.objects.filter(pk__in=[rows[1].pk, rows[3].pk]).update(
            is_archived=True,
            archive_changed_at=INSTANT,
            archive_changed_by=self.users["owner"],
        )
        generation = self.drain(self.prepare())
        self.assertEqual(saved_trips(self.case).get().source_count, 3)
        self.assertEqual(saved_trips(self.case, state="archived").get().source_count, 2)
        self.assertEqual(saved_trips(self.case, state="all").get().source_count, 5)
        trip = saved_trips(
            self.case, start=rows[2].observed_at, end=rows[2].observed_at
        ).get()
        self.assertEqual(trip.start_at, rows[0].observed_at)
        self.assertEqual(trip.end_at, rows[-1].observed_at)
        self.assertEqual(generation.metrics["archived"]["observations"], 2)

    def test_current_generations_reject_corrupt_provenance_and_archive_drift(self):
        rows = self.observations(2)
        self.drain(self.prepare())
        self.assertTrue(current_generations(self.case).exists())
        VehicleLocationEvidence.objects.filter(pk=rows[0].pk).update(
            is_archived=True,
            archive_changed_at=INSTANT,
            archive_changed_by=self.users["owner"],
        )
        self.assertFalse(current_generations(self.case).exists())
        VehicleLocationEvidence.objects.filter(pk=rows[0].pk).update(is_archived=False)
        other = Vehicle.objects.create(case=self.other_case, model="Private van")
        VehicleLocationEvidence.objects.filter(pk=rows[0].pk).update(vehicle=other)
        self.assertFalse(saved_trips(self.case).exists())

    def test_prepare_is_idempotent_and_source_change_supersedes_old_delivery(self):
        self.observations(2)
        generation = self.prepare()
        self.assertEqual(self.prepare().pk, generation.pk)
        process_trips(str(generation.pk), str(generation.token))
        generation.refresh_from_db()
        token = str(generation.token)
        replacement = invalidate_vehicle_trips(
            self.users["owner"], self.case, [self.vehicle.pk]
        )[0]
        self.assertNotEqual(replacement.pk, generation.pk)
        self.assertEqual(process_trips(str(generation.pk), token), "unavailable")
        generation.refresh_from_db()
        self.assertEqual(generation.status, "superseded")
        self.drain(replacement)

    def test_explicit_queued_retry_replaces_lost_delivery_without_duplicate_results(
        self,
    ):
        self.observations(2)
        generation = self.prepare()
        replacement = prepare_case_trips(self.users["owner"], self.case.pk, retry=True)[
            0
        ]
        self.assertEqual(replacement.attempts, 2)
        self.assertNotEqual(replacement.pk, generation.pk)
        self.assertEqual(
            process_trips(str(generation.pk), str(generation.token)), "unavailable"
        )
        self.drain(replacement)
        self.assertEqual(saved_trips(self.case).count(), 1)
        self.assertEqual(
            VehicleTripGeneration.objects.filter(case=self.case).count(), 2
        )

    def test_revocation_between_chunks_keeps_staged_results_unpublished(self):
        self.observations(502)
        generation = self.prepare("editor")
        process_trips(str(generation.pk), str(generation.token))
        generation.refresh_from_db()
        self.assertEqual(generation.processed_sources, 500)
        self.case.memberships.filter(user=self.users["editor"]).delete()
        self.assertEqual(
            process_trips(str(generation.pk), str(generation.token)), "failed"
        )
        self.assertFalse(saved_trips(self.case).exists())
        generation.refresh_from_db()
        self.assertEqual(generation.error_code, "access_removed")
        self.assertEqual(generation.inputs.count(), 500)

    def test_revocation_prevents_processing_and_another_editor_can_retry(self):
        self.observations(2)
        generation = self.prepare("editor")
        self.case.memberships.filter(user=self.users["editor"]).delete()
        self.assertEqual(
            process_trips(str(generation.pk), str(generation.token)), "failed"
        )
        generation.refresh_from_db()
        self.assertEqual(generation.error_code, "access_removed")
        replacement = prepare_case_trips(self.users["owner"], self.case.pk, retry=True)[
            0
        ]
        self.assertEqual(replacement.attempts, 2)
        self.drain(replacement)
        with self.assertRaises(PermissionDenied):
            self.prepare("viewer")

    def test_failures_roll_back_staging_and_retry_limit_is_three(self):
        self.observations(2)
        generation = self.prepare()
        with patch(
            "geo.analysis.deterministic.trips.Segmenter.process",
            side_effect=RuntimeError("synthetic"),
        ):
            for attempt in range(1, 4):
                self.assertEqual(
                    process_trips(str(generation.pk), str(generation.token)), "failed"
                )
                self.assertFalse(generation.inputs.exists())
                if attempt < 3:
                    generation = prepare_case_trips(
                        self.users["owner"], self.case.pk, retry=True
                    )[0]
        with self.assertRaises(ValidationError):
            prepare_case_trips(self.users["owner"], self.case.pk, retry=True)

    def test_running_recovery_requires_worker_confirmation_and_invalidations_are_atomic(
        self,
    ):
        self.observations(2)
        generation = self.prepare()
        VehicleTripGeneration.objects.filter(pk=generation.pk).update(status="running")
        with self.assertRaises(ValidationError):
            prepare_case_trips(self.users["owner"], self.case.pk, retry=True)
        replacement = prepare_case_trips(
            self.users["owner"], self.case.pk, retry=True, worker_stopped=True
        )[0]
        self.drain(replacement)
        state = VehicleTripState.objects.get(vehicle=self.vehicle)
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                invalidate_vehicle_trips(
                    self.users["owner"], self.case, [self.vehicle.pk]
                )
                raise RuntimeError("rollback")
        state.refresh_from_db()
        self.assertEqual(state.current_generation_id, replacement.pk)

    def test_viewer_deletion_invalidation_never_queues_as_viewer(self):
        self.observations(2)
        self.drain(self.prepare())
        self.users["viewer"].is_superuser = True
        self.assertEqual(
            invalidate_vehicle_trips(
                self.users["viewer"], self.case, [self.vehicle.pk]
            ),
            [],
        )
        self.assertFalse(saved_trips(self.case).exists())
        self.assertEqual(VehicleTripState.objects.get(vehicle=self.vehicle).revision, 1)

    def test_local_address_preparation_continues_in_bounded_runs(self):
        self.observations(61)
        with patch("geo.map_geocoding.local_geocoder_ready", return_value=True):
            generation = self.drain(self.prepare())
        self.assertEqual(generation.geocode_status, "queued")
        with (
            patch("geo.map_geocoding.local_geocoder_ready", return_value=True),
            patch("geo.map_geocoding.address_estimates"),
            patch("geo.map_geocoding.queue_addresses") as queue,
        ):
            for _ in range(5):
                generation.refresh_from_db()
                if generation.geocode_status != "queued":
                    break
                prepare_addresses(str(generation.pk), str(generation.token))
        generation.refresh_from_db()
        self.assertEqual(generation.geocode_status, "complete")
        self.assertEqual(
            [len(call.args[2]) for call in queue.call_args_list], [25, 25, 11]
        )
        self.assertEqual(generation.geocode_cursor, 61)

    @override_settings(**LOCAL_GEOCODER)
    def test_automatic_local_lookup_runs_are_bounded_and_terminal_results_reused(self):
        self.observations(27)
        generation = self.drain(self.prepare())
        for _ in range(3):
            generation.refresh_from_db()
            if generation.geocode_status != "queued":
                break
            prepare_addresses(str(generation.pk), str(generation.token))
        runs = AnalysisRun.objects.filter(case=self.case, operation="geocode_locations")
        self.assertEqual(sorted(len(run.inputs) for run in runs), [2, 25])
        with patch(
            "geo.analysis.external.geocoding.Geocoder.lookup", return_value=[]
        ) as lookup:
            for run in runs:
                process_run(str(run.pk))
        self.assertEqual(lookup.call_count, 27)
        replacement = self.drain(
            invalidate_vehicle_trips(self.users["owner"], self.case, [self.vehicle.pk])[
                0
            ]
        )
        for _ in range(3):
            replacement.refresh_from_db()
            if replacement.geocode_status != "queued":
                break
            prepare_addresses(str(replacement.pk), str(replacement.token))
        self.assertEqual(runs.count(), 2)
        replacement.refresh_from_db()
        self.assertEqual(replacement.geocode_status, "complete")

    def test_address_preparation_can_enable_later_and_fail_recoverably(self):
        self.observations(2)
        generation = self.drain(self.prepare())
        self.assertEqual(generation.geocode_status, "disabled")
        with override_settings(**LOCAL_GEOCODER):
            queued = self.prepare()
            self.assertEqual(queued.pk, generation.pk)
            self.assertEqual(queued.geocode_attempts, 1)
            with patch(
                "geo.map_geocoding.address_estimates",
                side_effect=RuntimeError("synthetic"),
            ):
                self.assertEqual(
                    prepare_addresses(str(queued.pk), str(queued.token)), "failed"
                )
            generation.refresh_from_db()
            self.assertEqual(generation.geocode_error_code, "preparation_failed")
            self.assertEqual(generation.status, "complete")
            self.assertEqual(self.prepare().geocode_status, "failed")
            resumed = prepare_case_trips(
                self.users["editor"], self.case.pk, retry=True
            )[0]
            self.assertEqual(resumed.geocode_attempts, 2)
            self.assertEqual(resumed.geocode_requested_by_id, self.users["editor"].pk)
            with patch("geo.map_geocoding.queue_addresses"):
                self.assertEqual(
                    prepare_addresses(str(resumed.pk), str(resumed.token)), "complete"
                )

    def test_queued_and_running_address_preparation_recovery_replaces_old_tokens(self):
        self.observations(2)
        generation = self.drain(self.prepare())
        with override_settings(**LOCAL_GEOCODER):
            queued = self.prepare()
            old_token = str(queued.token)
            replaced = prepare_case_trips(
                self.users["owner"], self.case.pk, retry=True
            )[0]
            self.assertEqual(replaced.geocode_attempts, 2)
            self.assertNotEqual(str(replaced.token), old_token)
            self.assertEqual(
                prepare_addresses(str(queued.pk), old_token), "unavailable"
            )
            VehicleTripGeneration.objects.filter(pk=replaced.pk).update(
                geocode_status="running"
            )
            with self.assertRaises(ValidationError):
                prepare_case_trips(self.users["owner"], self.case.pk, retry=True)
            recovered = prepare_case_trips(
                self.users["owner"], self.case.pk, retry=True, worker_stopped=True
            )[0]
            self.assertEqual(recovered.geocode_attempts, 3)
            with patch("geo.map_geocoding.queue_addresses"):
                self.assertEqual(
                    prepare_addresses(str(recovered.pk), str(recovered.token)),
                    "complete",
                )
        generation.refresh_from_db()
        self.assertEqual(generation.status, "complete")
