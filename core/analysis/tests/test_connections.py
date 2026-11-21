"""Connection groups preserve original identities and conservative coverage."""

from django.test import TestCase
from django.urls import reverse

from core.analysis.services import snapshot_sources
from core.analysis.tests.test_catalogue import CatalogueTests
from core.models import AnalysisFindingSource, AnalysisReview
from core.testing import EvidenceViewTestMixin
from text.models import TextEvidence


class ConnectionTests(EvidenceViewTestMixin, TestCase):
    publish = CatalogueTests.publish

    def setUp(self):
        super().setUp()
        self.note = self.text()
        self.url = reverse(
            "core:analysis:connections", kwargs={"case_id": self.case.pk}
        )
        self.login("viewer")

    def match(self, original, *, status="complete", parameters=None):
        inputs = snapshot_sources(self.case, [{"kind": "text", "id": original.pk}])
        return self.publish(
            sources=inputs,
            items=[
                {
                    "label": "Email",
                    "text": "Shared@example.test",
                    "values": {"raw": "Shared@EXAMPLE.TEST"},
                    "source_ids": [inputs[0]["id"]],
                }
            ],
            status=status,
            parameters=parameters,
        )

    def test_multiple_runs_for_one_original_are_not_a_connection(self):
        self.match(self.note, parameters={"version": 1})
        self.match(self.note, parameters={"version": 2})
        response = self.client.get(self.url)
        self.assertEqual(response.context["page_obj"].paginator.count, 0)

    def test_group_collapses_versions_and_discloses_duplicates_and_coverage(
        self,
    ):
        self.match(self.note, parameters={"version": 1})
        latest = self.match(self.note, parameters={"version": 2})
        other = self.text()
        self.match(other, status="partial")
        AnalysisReview.objects.create(
            case=self.case,
            result=latest,
            reviewed_by=self.users["owner"],
            decision="rejected",
            item_index=0,
        )
        response = self.client.get(self.url)
        groups = response.context["groups"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["original_count"], 2)
        self.assertTrue(groups[0]["duplicate_checksums"])
        self.assertContains(response, "Includes partial analysis")
        detail = self.client.get(groups[0]["url"])
        self.assertEqual(detail.context["page_obj"].paginator.count, 2)
        self.assertNotContains(detail, "Finding review")
        self.assertContains(detail, "Partial coverage")
        self.assertContains(detail, "Copies are not independent corroboration")

    def test_changed_source_withholds_identifier_and_corrupt_projection(self):
        self.match(self.note)
        other = self.text()
        latest = self.match(other)
        TextEvidence.objects.filter(pk=other.pk).update(body="Changed")
        response = self.client.get(self.url)
        self.assertContains(response, "Candidate group unavailable")
        self.assertNotContains(response, "Shared@example.test")
        detail = self.client.get(response.context["groups"][0]["url"])
        self.assertEqual(sum(row["unavailable"] for row in detail.context["rows"]), 1)
        AnalysisFindingSource.objects.filter(finding__result=latest).update(
            evidence_id=self.note.pk
        )
        self.assertEqual(
            self.client.get(self.url).context["page_obj"].paginator.count, 0
        )

    def test_case_isolation_and_no_matches_coverage_message(self):
        response = self.client.get(self.url)
        self.assertContains(response, "does not establish that no connection exists")
        self.assertContains(response, "Coverage and interpretation", count=1)
        self.login("outsider")
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.login("owner")
        self.assertEqual(
            self.client.get(
                self.url, {"key": "invalid", "identifier": "email"}
            ).status_code,
            404,
        )

    def test_latest_empty_run_removes_connection_without_losing_history(self):
        self.match(self.note)
        other = self.text()
        result = self.match(other)
        self.publish(sources=result.run.inputs, items=[], status="empty")
        self.assertEqual(
            self.client.get(self.url).context["page_obj"].paginator.count, 0
        )
        self.assertEqual(
            self.client.get(self.url, {"history": "all"})
            .context["page_obj"]
            .paginator.count,
            1,
        )

    def test_identifier_filter_searches_actual_values_and_preserves_group_scope(self):
        self.match(self.note)
        self.match(self.text())
        response = self.client.get(
            self.url, {"identifier_type": "email", "q": "shared"}
        )
        self.assertContains(response, "Find an identifier")
        self.assertNotContains(response, 'name="method"')
        group = response.context["groups"][0]
        detail = self.client.get(group["url"])
        self.assertEqual(detail.context["page_obj"].paginator.count, 2)
        self.assertContains(detail, "Back to repeated identifiers")
        empty = self.client.get(self.url, {"identifier_type": "phone", "q": "shared"})
        self.assertContains(empty, "No repeated identifier candidates")
        self.assertNotContains(empty, "Check identifiers in text")
        self.login("editor")
        self.assertContains(
            self.client.get(self.url, {"q": "invalid"}), "Check identifiers in text"
        )
