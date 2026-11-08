"""Deterministic call report calculations, provenance, and scoped filters."""

from datetime import UTC, datetime, timedelta, timezone

from django.core.exceptions import ValidationError
from django.http import Http404
from django.test import TestCase

from core.analysis.reports import report
from core.testing import EvidenceViewTestMixin
from people.models import Person
from phone.models import FaceTimeEvidence, PhoneCallEvidence

INSTANT = datetime(2026, 10, 27, 12, tzinfo=UTC)


class CallReportTests(EvidenceViewTestMixin, TestCase):
    def call(self, model=PhoneCallEvidence, **values):
        if values.get("is_archived"):
            values |= {
                "archive_changed_at": INSTANT,
                "archive_changed_by": self.users["owner"],
            }
        return model.objects.create(
            **(
                {
                    "case": self.case,
                    "uploaded_by": self.users["owner"],
                    "title": "Synthetic call",
                    "sender": self.person,
                    "recipient": self.person,
                }
                | values
            )
        )

    def test_calls_distinguish_unknown_zero_types_times_and_source_records(self):
        self.call(duration=None, occurred_at=None)
        zero = self.call(duration=timedelta(), occurred_at=INSTANT)
        video = self.call(
            FaceTimeEvidence,
            duration=timedelta(seconds=90),
            occurred_at=INSTANT.astimezone(timezone(timedelta(hours=-4))),
            is_archived=True,
        )
        result = report(self.case, "calls", state="all")
        metrics = result["metrics"]
        self.assertEqual(metrics["Selected records"], 3)
        self.assertEqual(metrics["Phone calls"], 2)
        self.assertEqual(metrics["FaceTime calls"], 1)
        self.assertEqual(metrics["Unknown duration"], 1)
        self.assertEqual(metrics["Zero duration"], 1)
        self.assertEqual(metrics["Reported duration total (seconds, known only)"], 90)
        self.assertEqual(metrics["Reported duration median (seconds, known only)"], 45)
        self.assertEqual(metrics["Unknown event time in selection"], 1)
        self.assertIn("12:00–12:59: 2", metrics["Recorded UTC hour counts"])
        self.assertEqual(len(result["items"]), 3)
        self.assertTrue(all(item["url"] for item in result["items"]))
        self.assertEqual(
            next(x for x in result["items"] if str(zero.pk) in x["url"])["values"][
                "Reported duration (seconds)"
            ],
            0,
        )
        self.assertIn(str(video.pk), str(result["items"]))
        self.assertEqual(report(self.case, "calls")["metrics"]["Selected records"], 2)
        selected = report(self.case, "calls", state="all", start=INSTANT, end=INSTANT)
        self.assertEqual(selected["metrics"]["Selected records"], 2)
        self.assertEqual(
            selected["metrics"]["Missing event time before period filter"], 1
        )
        self.assertIn("excluded", " ".join(selected["warnings"]))

    def test_same_named_participants_are_not_collapsed_to_one_pair(self):
        other = Person.objects.create(case=self.case, first_name=self.person.first_name)
        self.call()
        self.call(recipient=other)
        result = report(self.case, "calls", participant_id=self.person.pk)
        pair_counts = result["metrics"]["Participant pair counts"]
        self.assertIn(str(other.pk), pair_counts)
        self.assertIn(str(self.person.pk), pair_counts)
        self.assertEqual(pair_counts.count(": 1"), 2)

    def test_exact_participant_pair_includes_both_directions_and_self_pairs(self):
        second = Person.objects.create(case=self.case, first_name="Second person")
        third = Person.objects.create(case=self.case, first_name="Third person")
        forward = self.call(recipient=second, occurred_at=INSTANT)
        reverse = self.call(FaceTimeEvidence, sender=second)
        excluded = self.call(recipient=third)
        self_call = self.call()
        result = report(
            self.case,
            "calls",
            participant_id=self.person.pk,
            other_participant_id=second.pk,
        )
        self.assertEqual(result["metrics"]["Matching records"], 2)
        self.assertEqual(result["metrics"]["Selected records"], 2)
        self.assertEqual(result["metrics"]["Phone calls"], 1)
        self.assertEqual(result["metrics"]["FaceTime calls"], 1)
        self.assertEqual(result["metrics"]["Other participant filter"], str(second.pk))
        sources = " ".join(item["url"] for item in result["items"])
        self.assertIn(str(forward.pk), sources)
        self.assertIn(str(reverse.pk), sources)
        self.assertNotIn(str(excluded.pk), sources)
        self.assertNotIn(str(self_call.pk), sources)
        one_person = report(self.case, "calls", participant_id=self.person.pk)
        self.assertEqual(one_person["metrics"]["Selected records"], 4)
        self_pair = report(
            self.case,
            "calls",
            participant_id=self.person.pk,
            other_participant_id=self.person.pk,
        )
        self.assertEqual(self_pair["metrics"]["Selected records"], 1)
        self.assertIn(str(self_call.pk), self_pair["items"][0]["url"])
        period = report(
            self.case,
            "calls",
            participant_id=self.person.pk,
            other_participant_id=second.pk,
            start=INSTANT,
        )
        self.assertEqual(period["metrics"]["Selected records"], 1)
        self.assertEqual(
            period["metrics"]["Missing event time before period filter"], 1
        )

    def test_pair_filter_requires_both_same_case_participants(self):
        foreign = Person.objects.create(case=self.other_case, first_name="Private")
        for identifier in (foreign.pk, "not-a-uuid"):
            with self.subTest(identifier=identifier), self.assertRaises(Http404):
                report(
                    self.case,
                    "calls",
                    participant_id=self.person.pk,
                    other_participant_id=identifier,
                )
        with self.assertRaises(ValidationError):
            report(self.case, "calls", other_participant_id=self.person.pk)
        with self.assertRaises(ValidationError):
            report(
                self.case,
                "vehicle-coverage",
                participant_id=self.person.pk,
                other_participant_id=self.person.pk,
            )

    def test_reports_bound_selections_and_do_not_claim_whole_case_completeness(self):
        self.call()
        self.call()
        result = report(self.case, "calls", limit=1)
        self.assertEqual(result["metrics"]["Matching records"], 2)
        self.assertEqual(result["metrics"]["Selected records"], 1)
        self.assertTrue(result["metrics"]["Truncated"])
        self.assertIn("Only the first 1 of 2", result["warnings"][0])
