from datetime import UTC, datetime, timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from core.evidence_chat import corpus
from core.evidence_chat.trips import trip_materials
from core.testing import EvidenceViewTestMixin
from geo.models import VehicleLocationEvidence
from geo.trip_jobs import process_trips
from geo.trip_models import VehicleTripSource
from geo.trip_processing import prepare_case_trips
from phone.models import TextMessageEvidence

START = datetime(2026, 4, 12, 12, tzinfo=UTC)


@override_settings(PANDECT_GEOCODER_LOCAL=False)
class SavedTripChatTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.locations = [
            VehicleLocationEvidence.objects.create(
                case=self.case,
                uploaded_by=self.users["owner"],
                vehicle=self.vehicle,
                title=address,
                address=address,
                latitude=None,
                longitude=None,
                observed_at=START + timedelta(minutes=index * 5),
            )
            for index, address in enumerate(("Hill Street", "East Avenue", "Pier Road"))
        ]
        self.message = TextMessageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            sender=self.person,
            recipient=self.person,
            title="Delivery",
            body="The parcel is at the dock.",
            occurred_at=START + timedelta(minutes=9),
        )
        self.generation = self.publish(
            prepare_case_trips(self.users["owner"], self.case.pk)[0]
        )

    def publish(self, generation):
        for _ in range(4):
            process_trips(str(generation.pk), str(generation.token))
            generation.refresh_from_db()
            if generation.status == "complete":
                break
        self.assertEqual(generation.status, "complete", generation.error_code)
        return generation

    def material(self):
        (trip,) = trip_materials(
            self.case, "recent", through=timezone.now(), source=None, filters={}
        )
        return trip

    def test_outside_interval_and_mismatched_membership_are_excluded(self):
        late = TextMessageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            sender=self.person,
            recipient=self.person,
            title="Delivery",
            body="Dinner confirmed.",
            occurred_at=START + timedelta(days=1),
        )
        self.assertEqual(
            trip_materials(
                self.case,
                "recent",
                through=timezone.now(),
                source={
                    "surface": "original",
                    "kind": "text-message",
                    "pk": str(late.pk),
                },
                filters={},
            ),
            [],
        )
        material = self.material()
        self.assertIsNone(corpus.read_window(self.other_case, material.locator))
        self.assertIsNone(
            corpus.read_window(self.case, material.locator | {"start_at": "altered"})
        )
        item = self.generation.inputs.get(observation=self.locations[1])
        corrupt = VehicleTripSource.objects.create(
            case=self.other_case, trip_id=material.locator["pk"], input=item, ordinal=99
        )
        self.assertIsNone(corpus.read_window(self.case, material.locator))
        corrupt.delete()
        self.generation.inputs.filter(pk=item.pk).update(
            snapshot=item.snapshot | {"address": "Other"}
        )
        self.assertIsNone(corpus.read_window(self.case, material.locator))
