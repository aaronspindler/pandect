from datetime import UTC, datetime, timedelta

from django.test import TestCase
from django.urls import reverse

from core.analysis.navigation import finding_url
from core.analysis.services import start_run
from core.analysis.worker import process_run
from core.models import AnalysisFindingSource, AnalysisResult
from core.tests.testing import EvidenceViewTestMixin
from geo.chronology import JOURNEYS_PER_OBSERVATION, attach_candidate_journeys
from geo.models import PersonLocationEvidence


class JourneyChronologyTests(EvidenceViewTestMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        midnight = datetime(2026, 10, 20, tzinfo=UTC)
        cls.locations = [
            PersonLocationEvidence.objects.create(
                case=cls.case,
                uploaded_by=cls.users["owner"],
                title=f"Reading {index}",
                person=cls.person,
                latitude="44.000000",
                longitude=longitude,
                observed_at=midnight + timedelta(minutes=index * 10 - 5),
                collected_at=midnight + timedelta(days=1),
            )
            for index, longitude in enumerate(("-79.000000", "-79.100000"))
        ]

    def journey(self, **parameters):
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "candidate_trips",
            [
                {"kind": "person-location", "id": str(item.pk)}
                for item in self.locations
            ],
            parameters=parameters,
        )
        process_run(str(run.pk))
        run.refresh_from_db()
        self.assertEqual(run.status, "complete")
        return run

    def previews(self):
        rows = [
            {
                "kind": "person-location",
                "id": self.locations[-1].pk,
                "event_label": "Observed",
            }
        ]
        attach_candidate_journeys(self.case, rows)
        return rows[0].get("journeys", [])

    def test_saved_intervals_on_filtered_sources_keep_endpoints_and_alternatives(self):
        run = self.journey()
        alternate = self.journey(distance_m=300)
        self.login("viewer")
        url = reverse("core:evidence:chronology", args=[self.case.pk])
        response = self.client.get(url, {"start": "2026-10-20", "end": "2026-10-20"})
        rows = response.context["rows"]
        self.assertEqual([row["id"] for row in rows], [self.locations[-1].pk])
        self.assertEqual(
            {journey["url"] for journey in rows[0]["journeys"]},
            {finding_url(self.case.pk, saved.pk, 0) for saved in (run, alternate)},
        )
        self.assertContains(response, "Candidate journey", count=2)
        journey = rows[0]["journeys"][0]
        self.assertEqual(journey["start_at"], self.locations[0].observed_at)
        self.assertEqual(journey["end_at"], self.locations[-1].observed_at)
        self.assertEqual(journey["largest_gap"], timedelta(minutes=10))
        self.assertEqual(journey["version"], alternate.method_version)
        self.assertContains(
            response, "Browse all journey findings for this observation"
        )
        self.assertNotContains(
            self.client.get(url, {"category": "acquired"}), "Candidate journey"
        )
        PersonLocationEvidence.objects.filter(pk=self.locations[0].pk).update(
            is_archived=True,
            archive_changed_by=self.users["owner"],
            archive_changed_at=self.locations[0].observed_at,
        )
        self.assertEqual(self.previews(), [])

    def test_corrupt_anchor_or_cross_case_reference_suppresses_entire_interval(self):
        run = self.journey()
        self.assertEqual(len(self.previews()), 1)
        result = run.result
        data = result.data
        original = data["items"][0]["anchors"][0]
        data["items"][0]["anchors"][0] = data["items"][0]["anchors"][1]
        AnalysisResult.objects.filter(pk=result.pk).update(data=data)
        self.assertEqual(self.previews(), [])
        data["items"][0]["anchors"][0] = original
        AnalysisResult.objects.filter(pk=result.pk).update(data=data)
        AnalysisFindingSource.objects.filter(
            finding__result=result, evidence_id=self.locations[0].pk
        ).update(case=self.other_case)
        self.assertEqual(self.previews(), [])

    def test_superseded_runs_do_not_displace_distinct_alternatives_under_preview_limit(
        self,
    ):
        first = self.journey(distance_m=150)
        current = self.journey(distance_m=150)
        initial = self.previews()
        self.assertEqual(
            [j["url"] for j in initial], [finding_url(self.case.pk, current.pk, 0)]
        )
        self.assertNotEqual(current.pk, first.pk)
        alternatives = [
            self.journey(distance_m=200 + n) for n in range(JOURNEYS_PER_OBSERVATION)
        ]
        self.assertEqual(
            [j["url"] for j in self.previews()],
            [finding_url(self.case.pk, run.pk, 0) for run in reversed(alternatives)],
        )
