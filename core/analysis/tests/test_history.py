"""Automatic comparisons retain bounded case history and exact new anchors."""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from core.analysis.automatic import start_added_evidence_analysis
from core.analysis.bulk import prepare_case_analysis_batch
from core.analysis.tests.test_gateway import CONFIG
from core.analysis.tests.testing import external_grant_kwargs
from core.analysis.worker import process_run
from core.tests.testing import EvidenceViewTestMixin
from geo.models import VehicleLocationEvidence
from people.models import Person
from phone.models import PhoneCallEvidence, TextMessageEvidence
from text.models import TextEvidence
from vehicle.models import Vehicle


class IngestHistoryTests(EvidenceViewTestMixin, TestCase):
    def prepare(self, kind, item):
        batch, _ = start_added_evidence_analysis(
            self.users["owner"], self.case.pk, kind, item.pk
        )
        self.assertEqual(prepare_case_analysis_batch(batch.pk), "complete")
        return batch

    def test_communication_windows_use_matching_participants_and_nearest_sources(self):
        moment = timezone.now()
        peer = Person.objects.create(case=self.case, first_name="Peer")
        values = {
            "case": self.case,
            "uploaded_by": self.users["owner"],
            "title": "Synthetic communication",
            "sender": self.person,
            "recipient": peer,
        }
        message = TextMessageEvidence.objects.create(
            **values, body="First message near the recorded event.", occurred_at=moment
        )
        recent = TextMessageEvidence.objects.create(
            **values,
            body="Another relevant message.",
            occurred_at=moment - timedelta(minutes=42),
        )
        self.assertNotEqual(recent.pk, message.pk)
        TextMessageEvidence.objects.create(
            **values,
            body="Outside the session window.",
            occurred_at=moment - timedelta(minutes=61),
        )
        nearer = PhoneCallEvidence.objects.create(
            **(values | {"sender": peer, "recipient": self.person}),
            occurred_at=moment + timedelta(minutes=1),
        )
        PhoneCallEvidence.objects.create(
            **(values | {"recipient": self.person}),
            occurred_at=moment + timedelta(seconds=1),
        )
        PhoneCallEvidence.objects.create(
            **values, occurred_at=moment - timedelta(minutes=9)
        )
        PhoneCallEvidence.objects.create(
            **values, occurred_at=moment - timedelta(minutes=11)
        )
        with self.settings(PANDECT_ANALYSIS_MAX_SOURCES=2):
            batch = self.prepare("text-message", message)

        def ids(run):
            return [source["evidence_id"] for source in run.inputs]

        self.assertEqual(
            ids(batch.runs.get(operation="comm_patterns")),
            [str(message.pk), str(recent.pk)],
        )
        self.assertEqual(
            ids(batch.runs.get(operation="conversation_link")),
            [str(message.pk), str(nearer.pk)],
        )
        self.assertEqual(ids(batch.runs.get(operation="entities")), [str(message.pk)])

    @override_settings(PANDECT_AI_GATEWAY=CONFIG)
    def test_only_qualifying_delayed_reply_extends_automatic_session_history(self):
        external_grant_kwargs(self.case, "session_summaries")
        external_grant_kwargs(self.case, "session_summaries")
        moment = timezone.now()
        peer = Person.objects.create(case=self.case, first_name="Peer")
        values = {
            "case": self.case,
            "uploaded_by": self.users["owner"],
            "title": "Synthetic exchange",
        }
        question = TextMessageEvidence.objects.create(
            **values,
            sender=self.person,
            recipient=peer,
            body="Can you meet at six?",
            occurred_at=moment,
        )
        answer = TextMessageEvidence.objects.create(
            **values,
            sender=peer,
            recipient=self.person,
            body="Yes, six works.",
            occurred_at=moment + timedelta(hours=2),
        )
        unrelated = TextMessageEvidence.objects.create(
            **values,
            sender=self.person,
            recipient=peer,
            body="A different subject.",
            occurred_at=moment + timedelta(hours=4),
        )
        batch = self.prepare("text-message", answer)
        for op in ("comm_patterns", "session_summaries"):
            run = batch.runs.get(operation=op)
            ids = {source["evidence_id"] for source in run.inputs}
            self.assertEqual(ids, {str(question.pk), str(answer.pk)})
            self.assertNotIn(str(unrelated.pk), ids)
        self.assertEqual(
            batch.runs.get(operation="content_events").inputs[0]["evidence_id"],
            str(answer.pk),
        )

    def test_wording_shortlist_omits_earlier_only_pairs_from_results(self):
        def text(body, **values):
            return TextEvidence.objects.create(
                case=values.pop("case", self.case),
                uploaded_by=self.users["owner"],
                title="Synthetic evidence",
                body=body,
            )

        old_words = (
            "Cobalt signal carries older matching material one group that repeats"
        )
        old_left, old_right = text(old_words), text(old_words)
        words = "Cobalt contact met there tomorrow seeking approval for delivery today"
        peer = text(words)
        anchor = text(words)
        other = text(words, case=self.other_case)
        self.assertNotEqual(other.case_id, anchor.case_id)
        self.case.external_ai_enabled = True
        self.case.save(update_fields=["external_ai_enabled"])
        passages = [
            ({"dependencies": [{"kind": "text", "id": str(item.pk)}]}, 0)
            for item in (old_left, old_right, other, peer)
        ]
        with patch("core.case_search.hosted.search", return_value=passages):
            batch = self.prepare("text", anchor)
        run = batch.runs.get(operation="copied_text")
        self.assertEqual(
            {source["evidence_id"] for source in run.inputs},
            set(map(str, (anchor.pk, peer.pk, old_left.pk, old_right.pk))),
        )
        process_run(run.pk)
        run.refresh_from_db()
        result = run.result.data
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(
            set(result["items"][0]["source_ids"]),
            {
                run.inputs[0]["id"],
                next(
                    source["id"]
                    for source in run.inputs
                    if source["evidence_id"] == str(peer.pk)
                ),
            },
        )
        self.assertEqual(result["metrics"]["Comparison coverage"]["Earlier sources"], 3)

    def test_vehicle_movements_use_only_recent_same_vehicle_history(self):
        other = Vehicle.objects.create(case=self.case, model="Different van")
        moment = timezone.now()
        values = {
            "case": self.case,
            "uploaded_by": self.users["owner"],
            "title": "Synthetic GPS location",
            "vehicle": self.vehicle,
            "latitude": "43.640000",
            "longitude": "-79.380000",
        }
        anchor = VehicleLocationEvidence.objects.create(**values, observed_at=moment)
        prior = VehicleLocationEvidence.objects.create(
            **values, observed_at=moment - timedelta(days=29)
        )
        VehicleLocationEvidence.objects.create(
            **values, observed_at=moment - timedelta(days=31)
        )
        VehicleLocationEvidence.objects.create(
            **(values | {"vehicle": other}), observed_at=moment - timedelta(minutes=1)
        )
        batch = self.prepare("vehicle-location", anchor)
        comparison = batch.runs.get(operation="movement_routines")
        self.assertEqual(
            {source["evidence_id"] for source in comparison.inputs},
            {str(anchor.pk), str(prior.pk)},
        )
        quality = batch.runs.get(operation="location_quality")
        self.assertEqual(
            [source["evidence_id"] for source in quality.inputs], [str(anchor.pk)]
        )
