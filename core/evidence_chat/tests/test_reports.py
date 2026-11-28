"""Saved timing values preserve their exact selection and citation."""

from datetime import UTC, datetime, timedelta

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from core.analysis.catalogue import project_result
from core.analysis.source_resolution import resolve_inputs
from core.analysis.source_selection import snapshot_sources
from core.evidence_chat import corpus, reports
from core.evidence_chat.corpus_navigation import linked_materials
from core.models import AnalysisResult, AnalysisRun
from core.tests.testing import EvidenceTestMixin
from people.models import Person
from phone.analysis.deterministic.conversations import interaction_patterns
from phone.models import TextMessageEvidence


class TimingReportTests(EvidenceTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        other = Person.objects.create(case=self.case, first_name="Mica")
        stamp = datetime(2026, 2, 17, 10, tzinfo=UTC)
        self.first = self.create_evidence(
            TextMessageEvidence,
            recipient=other,
            occurred_at=stamp,
            body="Private message one.",
        )
        self.second = self.create_evidence(
            TextMessageEvidence,
            sender=other,
            occurred_at=stamp + timedelta(minutes=1),
            body="Private message two.",
        )

    def save_report(self, messages, *, status="complete"):
        inputs = snapshot_sources(
            self.case, [{"kind": "text-message", "id": m.pk} for m in messages]
        )
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.owner,
            operation="comm_patterns",
            method_version="2",
            inputs=inputs,
            status=status,
        )
        data = interaction_patterns(resolve_inputs(run, self.case), {})
        result = AnalysisResult.objects.create(case=self.case, run=run, data=data)
        project_result(result)
        return result

    def test_reads_saved_measurements_and_reopens_the_exact_source(self):
        result = self.save_report([self.first, self.second])
        self.assertFalse(result.findings.exists())
        with CaptureQueriesContext(connection) as queries:
            (material,) = reports.timing_materials(self.case, through=timezone.now())
        for query in queries:
            self.assertNotIn('"phone_textmessageevidence"."body"', query["sql"])
            self.assertNotIn('"core_analysisresult"."provenance"', query["sql"])
            self.assertNotIn('"core_analysisresult"."data",', query["sql"])
        self.assertEqual(material.locator["result_id"], str(result.pk))
        self.assertEqual(
            corpus.original_keys(material.locator),
            {f"text-message:{self.first.pk}", f"text-message:{self.second.pk}"},
        )
        self.assertIn("Selected messages: 2", material.text)
        self.assertIn("Mica", material.text)
        self.assertNotIn("Private message", material.text)
        self.assertLessEqual(len(material.text), 900)
        quote = "Median response interval (seconds): 60.0"
        citation = corpus.cite(
            self.case, material.locator, material.text.index(quote), quote
        )
        self.assertEqual(citation["url"], material.url)
        self.assertIn("#processing-summary", citation["url"])
        self.assertNotIn(("report", ""), corpus.surfaces())
        self.assertIsNone(corpus.read_window(self.other_case, material.locator))
        self.second.is_archived = True
        self.second.archive_changed_by = self.owner
        self.second.archive_changed_at = timezone.now()
        self.second.save()
        self.assertIsNone(corpus.read_window(self.case, material.locator))
        self.assertEqual(
            reports.timing_materials(self.case, through=timezone.now()), []
        )

    def test_candidate_scope_and_historical_versions(self):
        original = self.save_report([self.first, self.second])
        cutoff = timezone.now()
        newer = self.save_report([self.first, self.second], status="partial")
        single = self.save_report([self.second])
        (old,) = reports.timing_materials(self.case, through=cutoff)
        self.assertEqual(old.locator["pk"], str(original.pk))
        current = reports.timing_materials(self.case, through=timezone.now())
        self.assertEqual(
            [m.locator["pk"] for m in current], [str(single.pk), str(newer.pk)]
        )
        self.assertIn("Response interval count: 0", current[0].text)
        self.assertIn("Unavailable", current[0].text)
        self.assertTrue(current[1].locator["gap"])
        source = {
            "surface": "original",
            "kind": "text-message",
            "pk": str(self.first.pk),
        }
        self.assertEqual(
            [
                m.locator
                for m in reports.timing_materials(
                    self.case, through=timezone.now(), sources=[source]
                )
            ],
            [current[1].locator],
        )
        self.assertEqual(
            reports.timing_materials(
                self.case, through=timezone.now(), sources=[source | {"kind": "text"}]
            ),
            [],
        )
        self.assertEqual(corpus.read_window(self.case, old.locator)["text"], old.text)

    def test_follow_report_only_opens_a_sample_from_the_exact_active_selection(self):
        selected = [self.first, self.second] + [
            self.create_evidence(TextMessageEvidence, body=f"Selected message {i}")
            for i in range(4)
        ]
        self.create_evidence(TextMessageEvidence, body="Outside the selection")
        before = timezone.now()
        self.save_report(selected)
        after = timezone.now()
        (report,) = reports.timing_materials(self.case, through=after)
        self.save_report(selected)

        def follow(*, case=None, through=after, filters=None, locator=None):
            return linked_materials(
                case or self.case,
                locator or report.locator,
                "Show supporting messages",
                through=through,
                filters=filters or {},
            )

        sample = follow()
        self.assertEqual(len(sample), 4)
        read_ids = {row.locator["pk"] for row in sample}
        self.assertLess(read_ids, {str(row.pk) for row in selected})
        self.assertEqual(follow(through=before), [])
        self.assertEqual(follow(case=self.other_case), [])
        self.assertEqual(follow(filters={"record_type": "text-message"}), [])
        self.assertEqual(follow(locator=report.locator | {"dependencies": []}), [])
        self.assertEqual(
            [row.locator["pk"] for row in follow(through=timezone.now())],
            [row.locator["pk"] for row in sample],
        )
        hidden = next(row for row in selected if str(row.pk) not in read_ids)
        hidden.is_archived = True
        hidden.archive_changed_at = timezone.now()
        hidden.archive_changed_by = self.owner
        hidden.save()
        self.assertEqual(follow(), [])
