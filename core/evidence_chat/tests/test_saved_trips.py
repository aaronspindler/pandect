import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase, override_settings
from django.utils import timezone

from core.analysis.tests.test_execution import LOCAL
from core.evidence.deletion import plan_deletion
from core.evidence_chat import ai, corpus, jobs, services
from core.evidence_chat.corpus_navigation import linked_materials
from core.evidence_chat.tests.test_engine import decision
from core.evidence_chat.trips import trip_materials
from core.models import AnalysisRun, ChatTurn
from core.testing import EvidenceViewTestMixin
from geo.models import VehicleLocationEvidence
from geo.trip_jobs import process_trips
from geo.trip_models import VehicleTripGeneration, VehicleTripSource
from geo.trip_processing import invalidate_vehicle_trips, prepare_case_trips
from geo.trip_retention import retire_snapshots
from phone.models import TextMessageEvidence

START = datetime(2026, 4, 12, 12, tzinfo=UTC)


@override_settings(PANDECT_LOCAL_AI=LOCAL, PANDECT_GEOCODER_LOCAL=False)
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

    def test_compare_message_to_saved_trip_then_rebuild_archive_and_delete(self):
        available = timezone.now()
        material = self.material()
        self.assertFalse(
            trip_materials(
                self.case, "unknown vehicle", through=available, source=None, filters={}
            )
        )
        self.assertFalse(
            trip_materials(self.case, "recent", through=START, source=None, filters={})
        )
        question = "Compare the dock parcel message with the saved possible trip."
        turn = services.submit_turn(
            self.users["editor"], self.case.pk, question, submission_id=uuid4()
        )
        generation_count = VehicleTripGeneration.objects.count()
        checks = AnalysisRun.objects.count()

        def infer(active, authorize, prompt, schema):
            authorize()
            records = json.loads(prompt.split("\n", 1)[1])["records"]
            message = next(
                row for row in records if row["type"] == "original/text-message"
            )
            trip = next(
                (row for row in records if row["type"] == "trip/possible-trip"), None
            )
            if trip is None:
                return decision(
                    "read_trip", "recent", missing="The saved observation interval."
                ) | {
                    "source": message["id"],
                }
            quote = "Last recorded place: Pier Road"
            return decision(
                "answer",
                claims=[
                    {
                        "text": quote,
                        "citations": [
                            {
                                "id": trip["id"],
                                "quote": quote,
                                "start": trip["text"].index(quote),
                            }
                        ],
                    },
                    {
                        "text": self.message.body,
                        "citations": [
                            {
                                "id": message["id"],
                                "quote": self.message.body,
                                "start": message["text"].index(self.message.body),
                            }
                        ],
                    },
                ],
            )

        with patch.object(ai, "infer", side_effect=infer) as calls:
            for _ in range(2):
                jobs.process_turn(turn.pk, turn.delivery_token)
                turn.refresh_from_db()
            self.assertEqual(turn.status, "complete", turn.error_code)
            self.assertEqual(calls.call_count, 2)
        answer = services.inspect_turn(turn)
        self.assertEqual(len(answer["claims"]), 2)
        self.assertEqual(answer["claims"][0]["citations"][0]["url"], material.url)
        self.assertEqual(VehicleTripGeneration.objects.count(), generation_count)
        self.assertEqual(AnalysisRun.objects.count(), checks)
        links = linked_materials(
            self.case,
            material.locator,
            "messages near those observations",
            through=available,
            filters={},
        )
        self.assertEqual(
            {m.locator["pk"] for m in links},
            {
                str(self.locations[0].pk),
                str(self.locations[-1].pk),
                str(self.message.pk),
            },
        )
        self.login("outsider")
        self.assertEqual(self.client.get(material.url).status_code, 404)
        self.login("viewer")
        self.assertContains(self.client.get(material.url), "East Avenue")
        next_generation = self.publish(
            invalidate_vehicle_trips(self.users["owner"], self.case, [self.vehicle.pk])[
                0
            ]
        )
        self.assertEqual(retire_snapshots(next_generation.pk), "complete")
        self.assertNotEqual(self.material().locator["pk"], material.locator["pk"])
        self.assertEqual(services.inspect_turn(turn), answer)
        self.assertContains(self.client.get(material.url), "Last recorded place")
        followup = services.submit_turn(
            self.users["editor"],
            self.case.pk,
            "Which of those happened first?",
            chat_id=turn.chat_id,
            submission_id=uuid4(),
        )
        with patch.object(
            ai,
            "infer",
            side_effect=AssertionError("Expected a recorded event comparison"),
        ):
            jobs.process_turn(followup.pk, followup.delivery_token)
        followup.refresh_from_db()
        self.assertEqual(followup.status, "complete", followup.error_code)
        first = services.inspect_turn(followup)["claims"][0]["citations"][0]
        self.assertEqual(
            first["quote"], f"First observation (UTC): {START.isoformat()}"
        )
        self.assertEqual(first["url"], material.url)

        update = services.submit_turn(
            self.users["editor"],
            self.case.pk,
            "What's new since answer 1?",
            chat_id=turn.chat_id,
            submission_id=uuid4(),
        )
        latest = self.material()

        def updated_trip(active, authorize, prompt, schema):
            authorize()
            rows = json.loads(prompt.split("\n", 1)[1])["records"]
            self.assertEqual([row["type"] for row in rows], ["trip/possible-trip"])
            quote = "Last recorded place: Pier Road"
            return decision(
                "answer",
                claims=[
                    {
                        "text": quote,
                        "citations": [
                            {
                                "id": rows[0]["id"],
                                "start": rows[0]["text"].index(quote),
                                "quote": quote,
                            }
                        ],
                    }
                ],
            )

        with patch.object(ai, "infer", side_effect=updated_trip) as calls:
            jobs.process_turn(update.pk, update.delivery_token)
        update.refresh_from_db()
        self.assertEqual(update.status, "complete", update.error_code)
        self.assertEqual(calls.call_count, 1)
        ref = services.inspect_turn(update)["claims"][0]["citations"][0]
        self.assertEqual(ref["url"], latest.url)
        self.assertEqual(services.inspect_turn(turn), answer)
        self.assertEqual(VehicleTripGeneration.objects.count(), generation_count + 1)
        plan = plan_deletion(self.locations[1], "vehicle-location")
        self.assertFalse(plan.blocker)
        self.assertIn(turn, plan.collector.data[ChatTurn])
        self.assertIn(followup, plan.collector.data[ChatTurn])
        VehicleLocationEvidence.objects.filter(pk=self.locations[1].pk).update(
            is_archived=True,
            archive_changed_at=available,
            archive_changed_by=self.users["editor"],
        )
        self.assertIsNone(services.inspect_turn(turn))
        self.assertIsNone(corpus.read_window(self.case, material.locator))
        self.assertEqual(self.client.get(material.url).status_code, 404)

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
