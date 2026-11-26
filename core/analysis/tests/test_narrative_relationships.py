from unittest.mock import Mock

from django.http import Http404
from django.test import TestCase
from django.utils import timezone

from cases.models import Case
from core.analysis.sources import Source
from core.models import (
    AnalysisResult,
    AnalysisReview,
    AnalysisRun,
    NarrativeRelationship,
)
from core.relationships import follow_leads, publish_relationships
from core.testing import EvidenceTestMixin
from text.analysis.relationships import extract_relationships


class NarrativeRelationshipsTests(EvidenceTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.quote = "Morgan denied meeting Robin."
        self.record = self.create_evidence(body=self.quote)
        self.source = Source(str(self.record.pk), "text", self.record)
        self.candidate = {
            "source_id": self.source.id,
            "subject": "Morgan",
            "object": "Robin",
            "predicate": "met",
            "qualification": "denied",
            "start": 0,
            "quote": self.quote,
        }
        Case.objects.filter(pk=self.case.pk).update(external_ai_enabled=True)

    def result(self, candidate):
        data = extract_relationships(
            [self.source], {}, infer=Mock(return_value={"relationships": [candidate]})
        )
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.owner,
            operation="narrative_relationships",
            status="complete",
            method_version="1",
        )
        return AnalysisResult.objects.create(case=self.case, run=run, data=data)

    def leads(self, actor=None):
        return follow_leads(
            self.case, actor=actor or self.owner, through=timezone.now(), term="Robin"
        )

    def test_denial_keeps_passage_and_rejection_survives_replay(self):
        result = self.result(self.candidate)
        publish_relationships(result, [self.source])
        (lead,) = self.leads()
        self.assertEqual(
            (lead["start"], lead["quote"], lead["qualification"]),
            (0, self.quote, "denied"),
        )
        self.assertEqual(lead["locator"]["evidence_id"], str(self.record.pk))
        self.assertEqual(lead["status"], "candidate")
        with self.assertRaises(Http404):
            self.leads(self.outsider)
        AnalysisReview.objects.create(
            case=self.case,
            reviewed_by=self.editor,
            result=result,
            item_index=0,
            decision="rejected",
        )
        publish_relationships(self.result(self.candidate), [self.source])
        self.assertEqual(NarrativeRelationship.objects.count(), 1)
        self.assertEqual(self.leads(), [])

    def test_changed_source_or_misquoted_model_output_is_excluded(self):
        self.assertEqual(self.result(self.candidate | {"start": 1}).data["items"], [])
        publish_relationships(self.result(self.candidate), [self.source])
        from core.evidence.retention import set_evidence_archive

        set_evidence_archive(
            self.editor, self.case.pk, "text", self.record.pk, archived=True
        )
        self.assertEqual(self.leads(), [])

    def test_reanalysis_refreshes_an_exact_passage_and_keeps_its_review(self):
        result = self.result(self.candidate)
        publish_relationships(result, [self.source])
        initial = self.leads()[0]["locator"]
        AnalysisReview.objects.create(
            case=self.case,
            reviewed_by=self.editor,
            result=result,
            item_index=0,
            decision="accepted",
        )
        self.record.title = "Updated statement title"
        self.record.save(update_fields=["title"])
        self.assertEqual(self.leads(), [])
        cutoff = timezone.now()
        publish_relationships(self.result(self.candidate), [self.source])
        (refreshed,) = self.leads()
        self.assertNotEqual(refreshed["locator"]["version"], initial["version"])
        self.assertEqual(
            (refreshed["quote"], refreshed["status"]), (self.quote, "accepted")
        )
        self.assertEqual(NarrativeRelationship.objects.count(), 1)
        self.assertEqual(
            follow_leads(self.case, through=cutoff, actor=self.owner, term="Robin"), []
        )
