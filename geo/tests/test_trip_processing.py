"""Saved full-history trips, resumable publication and current authorization."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django_tasks_db.models import DBTaskResult

from core.analysis.services import process_run
from core.models import AnalysisRun
from core.testing import EvidenceViewTestMixin
from geo.daily_chronology import attach_daily_location_details, daily_location_rows
from geo.models import LocationEvidence, PersonLocationEvidence, VehicleLocationEvidence
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
from geo.trip_retention import retire_snapshots
from vehicle.models import Vehicle

INSTANT = datetime(2026, 11, 15, 12, tzinfo=UTC)


@override_settings(PANDECT_GEOCODER_LOCAL=False)
class SavedTripProcessingTests(EvidenceViewTestMixin, TestCase):
    def observations(self, count, *, stationary=None, positive=None, start=INSTANT):
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
                observed_at=start + timedelta(minutes=index),
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

    def test_archived_inputs_are_excluded_and_time_filters_keep_whole_trips(self):
        rows = self.observations(5)
        VehicleLocationEvidence.objects.filter(pk__in=[rows[1].pk, rows[3].pk]).update(
            is_archived=True,
            archive_changed_at=INSTANT,
            archive_changed_by=self.users["owner"],
        )
        generation = self.drain(self.prepare())
        self.assertEqual(saved_trips(self.case).get().source_count, 3)
        self.assertEqual(generation.total_sources, 3)
        self.assertEqual(
            set(generation.inputs.values_list("observation_id", flat=True)),
            {rows[0].pk, rows[2].pk, rows[4].pk},
        )
        trip = saved_trips(
            self.case, start=rows[2].observed_at, end=rows[2].observed_at
        ).get()
        self.assertEqual(trip.start_at, rows[0].observed_at)
        self.assertEqual(trip.end_at, rows[-1].observed_at)
        self.assertEqual(generation.metrics["active"]["observations"], 3)

    def test_daily_rows_collapse_only_exact_trip_members_and_keep_overnight_endpoints(
        self,
    ):
        midnight = INSTANT.replace(hour=0)
        next_day = midnight + timedelta(days=1)
        samples = self.observations(4, start=midnight - timedelta(minutes=2))
        standalone = self.observations(1, start=midnight + timedelta(hours=4))[0]
        VehicleLocationEvidence.objects.filter(pk=samples[2].pk).update(
            is_archived=True,
            archive_changed_by=self.users["owner"],
            archive_changed_at=INSTANT,
        )
        common = {
            "case": self.case,
            "uploaded_by": self.users["owner"],
            "observed_at": midnight,
        }
        sighting = PersonLocationEvidence.objects.create(
            **common,
            title="Recorded lookout",
            person=self.person,
            latitude=1,
            longitude=-2,
        )
        place = LocationEvidence.objects.create(
            **common, title="Landing", address="South wharf"
        )
        LocationEvidence.objects.create(
            **(common | {"case": self.other_case}),
            title="Unrelated",
            address="Other wharf",
        )
        self.drain(self.prepare())
        trip = saved_trips(self.case).get()
        rows = list(daily_location_rows(self.case, midnight, next_day))
        attach_daily_location_details(self.case, rows)
        self.assertEqual(
            [row["id"] for row in rows], [place.pk, sighting.pk, trip.pk, standalone.pk]
        )
        self.assertEqual(rows[0]["detail"], "South wharf")
        self.assertIn(str(self.person), rows[1]["detail"])
        self.assertIn("1.000000, -2.000000", rows[1]["detail"])
        self.assertIn(str(self.vehicle), rows[3]["detail"])
        overnight = rows[2]
        self.assertEqual(overnight["at"], midnight)
        self.assertEqual(overnight["start_at"], samples[0].observed_at)
        self.assertEqual(overnight["end_at"], samples[-1].observed_at)
        self.assertTrue(overnight["continues_before"])
        self.assertFalse(overnight["continues_after"])
        self.assertIn("1 Moving Street → 4 Moving Street", overnight["detail"])
        self.assertIn("3 observations", overnight["detail"])
        self.assertEqual(
            overnight["url"], reverse("geo:trip_detail", args=(self.case.pk, trip.pk))
        )
        self.assertEqual(list(daily_location_rows(self.case, midnight, midnight)), [])
        earlier = list(
            daily_location_rows(self.case, midnight - timedelta(days=1), midnight)
        )
        self.assertEqual([row["id"] for row in earlier], [trip.pk])
        self.assertTrue(earlier[0]["continues_after"])

    def test_daily_rows_keep_originals_for_incomplete_or_stale_trips(self):
        midnight = INSTANT.replace(hour=0)
        samples = self.observations(3)
        self.drain(self.prepare())
        trips = saved_trips(self.case)
        trip_id = trips.get().pk
        trips.update(source_count=4)

        def visible():
            return [
                row["id"]
                for row in daily_location_rows(
                    self.case, midnight, midnight + timedelta(days=1)
                )
            ]

        self.assertEqual(visible(), [sample.pk for sample in samples])
        trips.update(source_count=3)
        self.assertEqual(visible(), [trip_id])
        VehicleTripState.objects.filter(case=self.case).update(current_generation=None)
        self.assertEqual(visible(), [sample.pk for sample in samples])

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

    def test_preparation_and_invalidation_scope_generations_to_the_vehicle_and_case(
        self,
    ):
        self.observations(2)
        generation = self.prepare()
        other_vehicle = Vehicle.objects.create(case=self.other_case, model="Other van")
        unrelated = VehicleTripGeneration.objects.create(
            case=self.other_case,
            vehicle=other_vehicle,
            state=generation.state,
            revision=generation.revision,
            method_version=generation.method_version,
            requested_by=self.users["outsider"],
        )
        self.assertEqual(self.prepare().pk, generation.pk)
        newest = invalidate_vehicle_trips(
            self.users["editor"], self.case, [self.vehicle.pk]
        )[0]
        self.assertEqual(newest.pk, generation.pk)
        unrelated.refresh_from_db()
        self.assertEqual(unrelated.status, "queued")
        self.assertEqual(unrelated.requested_by_id, self.users["outsider"].pk)
        self.assertEqual(unrelated.revision, 0)

    def test_resume_stops_before_publishing_when_checkpoint_source_cannot_be_verified(
        self,
    ):
        self.observations(2)
        generation = self.prepare()
        self.assertEqual(process_trips(generation.pk, generation.token), "queued")
        generation.refresh_from_db()
        carry = generation.carry
        carry["anchor"]["address"] = "Unverified checkpoint address"
        VehicleTripGeneration.objects.filter(pk=generation.pk).update(carry=carry)
        self.assertEqual(process_trips(generation.pk, generation.token), "failed")
        generation.refresh_from_db()
        self.assertEqual(generation.processed_sources, 2)
        self.assertEqual(generation.inputs.count(), 2)
        self.assertFalse(saved_trips(self.case).exists())

    def test_changes_before_first_claim_reuse_the_pending_delivery(self):
        self.observations(2)
        generation = self.prepare()
        self.observations(1, start=INSTANT + timedelta(minutes=2))
        newest = invalidate_vehicle_trips(
            self.users["editor"], self.case, [self.vehicle.pk]
        )[0]
        self.assertEqual(newest.pk, generation.pk)
        self.assertEqual(newest.token, generation.token)
        self.assertEqual(newest.total_sources, 3)
        self.assertEqual(newest.requested_by, self.users["editor"])
        self.assertEqual(self.drain(newest).inputs.count(), 3)

    def test_retirement_preserves_cited_history_and_deletes_only_bounded_slices(self):
        self.observations(2)
        cited = self.drain(self.prepare())
        AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.users["owner"],
            operation="journey_claims",
            method_version="1",
            parameters={
                "groups": [
                    {"origin": "trip", "generation_id": str(cited.pk), "members": []}
                ]
            },
        )
        uncited = self.drain(
            invalidate_vehicle_trips(self.users["owner"], self.case, [self.vehicle.pk])[
                0
            ]
        )
        current = self.drain(
            invalidate_vehicle_trips(self.users["owner"], self.case, [self.vehicle.pk])[
                0
            ]
        )
        with patch("geo.trip_retention.CHUNK_SIZE", 1):
            self.assertEqual(retire_snapshots(current.pk), "queued")
        self.assertEqual(uncited.inputs.count(), 1)
        self.assertEqual(cited.inputs.count(), 2)
        self.assertEqual(current.inputs.count(), 2)
        self.assertEqual(retire_snapshots(current.pk), "queued")
        self.assertEqual(retire_snapshots(current.pk), "queued")
        self.assertEqual(retire_snapshots(current.pk), "complete")
        self.assertFalse(uncited.inputs.exists())
        self.assertFalse(uncited.trips.exists())
        self.assertTrue(VehicleTripGeneration.objects.filter(pk=uncited.pk).exists())
        self.assertEqual(cited.inputs.count(), 2)
        self.assertEqual(current.inputs.count(), 2)

    def test_completed_retry_schedules_retirement_of_same_revision_staged_rows(self):
        self.observations(2)
        interrupted = self.prepare()
        with patch("geo.trip_jobs.CHUNK_SIZE", 1):
            self.assertEqual(
                process_trips(interrupted.pk, str(interrupted.token)), "queued"
            )
        self.assertEqual(interrupted.inputs.count(), 1)
        replacement = prepare_case_trips(self.users["owner"], self.case.pk, retry=True)[
            0
        ]
        self.assertEqual(replacement.revision, interrupted.revision)
        self.drain(replacement)
        self.assertTrue(
            DBTaskResult.objects.filter(
                task_path="geo.tasks.retire_trip_snapshots",
                args_kwargs={"args": [str(replacement.pk)], "kwargs": {}},
            ).exists()
        )
        self.assertEqual(retire_snapshots(replacement.pk), "queued")
        self.assertEqual(retire_snapshots(replacement.pk), "queued")
        self.assertEqual(retire_snapshots(replacement.pk), "complete")
        interrupted.refresh_from_db()
        self.assertEqual(interrupted.status, "superseded")
        self.assertFalse(interrupted.inputs.exists())
        self.assertFalse(interrupted.trips.exists())
        self.assertEqual(replacement.inputs.count(), 2)

    def test_retirement_keeps_cited_same_revision_method_history(self):
        self.observations(2)
        with (
            patch("geo.trip_processing.METHOD_VERSION", "vehicle-trips.v1"),
            patch("geo.trip_jobs.METHOD_VERSION", "vehicle-trips.v1"),
        ):
            older_method = self.drain(self.prepare())
        citation = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.users["owner"],
            operation="journey_claims",
            method_version="1",
            parameters={
                "groups": [{"origin": "trip", "generation_id": str(older_method.pk)}]
            },
        )
        current = self.drain(self.prepare())
        self.assertEqual(current.revision, older_method.revision)
        self.assertEqual(retire_snapshots(current.pk), "complete")
        self.assertEqual(older_method.inputs.count(), 2)
        citation.delete()
        self.assertEqual(retire_snapshots(current.pk), "queued")
        self.assertEqual(retire_snapshots(current.pk), "queued")
        self.assertEqual(retire_snapshots(current.pk), "complete")
        self.assertFalse(older_method.inputs.exists())
        self.assertFalse(older_method.trips.exists())
        self.assertEqual(current.inputs.count(), 2)

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

    def test_address_preparation_uses_the_verified_current_original(self):
        originals = self.observations(2)
        with patch("geo.map_geocoding.local_geocoder_ready", return_value=True):
            generation = self.drain(self.prepare())
        source = generation.inputs.first()
        generation.inputs.filter(pk=source.pk).update(
            snapshot=source.snapshot | {"address": "Unverified snapshot address"}
        )
        with (
            patch("geo.map_geocoding.local_geocoder_ready", return_value=True),
            patch("geo.map_geocoding.address_estimates"),
            patch("geo.map_geocoding.queue_addresses") as queue,
        ):
            self.assertEqual(
                prepare_addresses(generation.pk, generation.token), "complete"
            )
        self.assertEqual(
            [row["address"] for row in queue.call_args.args[2]],
            [original.address for original in originals],
        )

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
