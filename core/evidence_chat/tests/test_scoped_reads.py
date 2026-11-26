from datetime import timedelta
from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone

from core.analysis.catalogue import project_result
from core.analysis.source_selection import snapshot_sources
from core.evidence_chat import corpus_search
from core.evidence_chat.corpus import cite
from core.evidence_chat.corpus_navigation import linked_materials
from core.models import AnalysisResult, AnalysisRun
from core.testing import EvidenceTestMixin
from geo.models import LocationEvidence
from phone.models import PhoneCallEvidence


class ScopedReadTests(EvidenceTestMixin, TestCase):
    def finding(self, sources):
        inputs = snapshot_sources(
            self.case, [{"kind": "text", "id": row.pk} for row in sources]
        )
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.owner,
            operation="entities",
            inputs=inputs,
            status="complete",
            method_version="1",
        )
        item = {
            "label": "Synthetic reference",
            "text": "The dispatch cited the harbour.",
            "source_ids": [entry["id"] for entry in inputs],
            "values": {},
            "anchors": [],
        }
        project_result(
            AnalysisResult.objects.create(
                case=self.case, run=run, data={"items": [item]}
            )
        )

    def search(self, question, families, **kwargs):
        return corpus_search.ranked_windows(
            self.case,
            question,
            self.owner,
            False,
            10,
            None,
            surfaces_to_search=families,
            **kwargs,
        )

    def test_typed_search_excludes_other_families_before_querying(self):
        original = self.create_evidence(body="Dispatch from the harbour.")
        self.finding([original])
        hint = self.search(str(original.pk), {"original"})[0]
        related = Mock(
            side_effect=AssertionError("A typed read uses the selected index")
        )
        with (
            patch.object(corpus_search, "rows", wraps=corpus_search.rows) as rows,
            patch.object(corpus_search, "alternatives") as spelling,
            patch.object(corpus_search, "nearby_sources") as expansion,
        ):
            hits = self.search("harbour", {"finding"}, hints=[hint], related=related)
            self.assertEqual([loc["surface"] for loc, _ in hits], ["finding"])
            self.assertTrue(rows.call_args_list)
            self.assertEqual(
                {call.args[1] for call in rows.call_args_list}, {"finding"}
            )
            spelling.assert_not_called()
            expansion.assert_not_called()
            related.assert_not_called()

    def test_follow_finding_is_bounded_versioned_and_case_scoped(self):
        originals = [self.create_evidence(body=f"Dispatch {i}.") for i in range(6)]
        before = timezone.now()
        self.finding(originals)
        [(loc, _)] = self.search("harbour", {"finding"})
        now = timezone.now()
        rows = linked_materials(
            self.case, loc, "Which originals?", through=now, filters={}
        )
        self.assertEqual(len(rows), 4)
        self.assertEqual(len({row.locator["key"] for row in rows}), len(rows))
        for row in rows:
            self.assertTrue(row.text.startswith("Dispatch "))
            self.assertIsNotNone(cite(self.case, row.locator, 0, row.text[:8]))
        for case, anchor, cutoff, filters in (
            (self.other_case, loc, now, {}),
            (self.case, loc | {"version": "old"}, now, {}),
            (self.case, loc, before, {}),
            (self.case, loc, now, {"record_type": "finding"}),
        ):
            with self.subTest(case=case.pk, cutoff=cutoff, filters=filters):
                self.assertEqual(
                    linked_materials(case, anchor, "", through=cutoff, filters=filters),
                    [],
                )
        originals[0].is_archived = True
        originals[0].archive_changed_at = now
        originals[0].archive_changed_by = self.owner
        originals[0].save()
        self.assertEqual(
            linked_materials(self.case, loc, "", through=now, filters={}), []
        )

    def test_follow_chronology_selects_nearest_eligible_record(self):
        stamp = timezone.now()
        first = self.create_evidence(PhoneCallEvidence, occurred_at=stamp)
        self.create_evidence(LocationEvidence, observed_at=stamp + timedelta(seconds=1))
        later = self.create_evidence(
            PhoneCallEvidence, occurred_at=stamp + timedelta(minutes=2)
        )
        cutoff = timezone.now()
        self.create_evidence(
            PhoneCallEvidence, occurred_at=stamp + timedelta(seconds=2)
        )
        [(loc, _)] = self.search(str(first.pk), {"original"})
        linked = linked_materials(
            self.case,
            loc,
            "What happened after this?",
            through=cutoff,
            filters={"record_type": "phone-call"},
        )
        self.assertEqual([row.locator["pk"] for row in linked], [str(later.pk)])
