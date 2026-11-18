"""Timing-only observations stay inspectable without crowding default findings."""

import importlib
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.apps import apps
from django.db import connection
from django.http import QueryDict
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from cases.models import CaseMembership
from core.analysis.catalogue import catalogue_page, project_result
from core.analysis.projection import finding_values, is_timing_only
from core.analysis.services import snapshot_sources
from core.models import (
    AnalysisFindingSource,
    AnalysisResult,
    AnalysisReview,
    AnalysisRun,
)
from core.testing import EvidenceViewTestMixin
from phone.models import TextMessageEvidence


def timing_item(**values):
    return {
        "label": "Candidate exchange sequence",
        "text": "Recorded temporal adjacency",
        "values": {
            "before": {"text": "Synthetic first message"},
            "after": {"text": "Synthetic second message"},
            "elapsed_seconds": 16.0,
            "shared_words": [],
            **values,
        },
    }


class TimingClassificationTests(SimpleTestCase):
    def test_only_explicit_timing_only_shape_is_suppressed(self):
        preserved = [
            None,
            {},
            {"label": "Candidate exchange sequence", "values": []},
            timing_item(status="unresolved"),
            timing_item(shared_words=["harbour"]),
            timing_item(shared_words=None),
            timing_item(before=None),
            timing_item(after=[]),
            timing_item(elapsed_seconds=True),
            timing_item(elapsed_seconds="16"),
            timing_item(elapsed_seconds=None),
            timing_item(elapsed_seconds=-1),
            timing_item(elapsed_seconds=float("inf")),
            timing_item(elapsed_seconds=float("nan")),
            timing_item(before={"reply_to_id": "native-before"}),
            timing_item(after={"reply_to_id": "native-after"}),
        ]
        missing_words = timing_item()
        del missing_words["values"]["shared_words"]
        preserved.append(missing_words)
        frozen = importlib.import_module("core.migrations.0006_timing_only_findings")
        for item in preserved:
            with self.subTest(item=item):
                self.assertFalse(is_timing_only("conversation_link", item))
                self.assertFalse(frozen.is_timing_only(item))
        for item in (
            timing_item(),
            timing_item(elapsed_seconds=0),
            timing_item(before={"native_thread_id": "thread"}),
        ):
            with self.subTest(item=item):
                self.assertTrue(is_timing_only("conversation_link", item))
                self.assertTrue(frozen.is_timing_only(item))
                self.assertFalse(is_timing_only("sessions", item))

    def test_projection_preserves_canonical_data_and_plate_classification(self):
        item = timing_item()
        original = deepcopy(item)
        self.assertTrue(finding_values("conversation_link", item)["weak"])
        self.assertEqual(item, original)
        self.assertFalse(
            finding_values("conversation_link", timing_item(status="unresolved"))[
                "weak"
            ]
        )
        self.assertTrue(
            finding_values("plate_ocr", {"values": {"Weak plate region": True}})["weak"]
        )


class TimingCatalogueTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        message = TextMessageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic source message",
            body="Synthetic original",
            sender=self.person,
            recipient=self.person,
        )
        self.inputs = snapshot_sources(
            self.case, [{"kind": "text-message", "id": message.pk}]
        )
        self.catalogue_url = reverse("core:analysis:browse", args=[self.case.pk])

    def publish(self, items, *, operation="conversation_link", parameters=None):
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.users["owner"],
            operation=operation,
            method_version="1",
            inputs=self.inputs,
            parameters=parameters or {},
            status="complete",
        )
        result = AnalysisResult.objects.create(
            case=self.case,
            run=run,
            data={
                "items": [
                    item | {"source_ids": [self.inputs[0]["id"]]} for item in items
                ]
            },
        )
        # Emulate the catalogue written before timing-only output stayed in the run.
        with patch("core.analysis.catalogue.is_timing_only", return_value=False):
            project_result(result)
        return result

    def review(self, result, decision, index=0):
        return AnalysisReview.objects.create(
            case=self.case,
            result=result,
            reviewed_by=self.users["owner"],
            decision=decision,
            item_index=index,
            comment="Synthetic review reason",
        )

    def test_default_filter_precedes_pagination_and_keeps_original_indices(self):
        self.publish(
            [timing_item() for _ in range(28)]
            + [timing_item(shared_words=["harbour"]) for _ in range(26)]
        )
        first = catalogue_page(self.case, QueryDict())
        self.assertEqual(first["timing_only_count"], 28)
        self.assertEqual(first["page_obj"].paginator.count, 26)
        self.assertEqual(
            [row["item_index"] for row in first["rows"]], list(range(28, 53))
        )
        last = catalogue_page(self.case, QueryDict("page=2"))
        self.assertEqual([row["item_index"] for row in last["rows"]], [53])
        shown = catalogue_page(self.case, QueryDict("weak=1"))
        self.assertEqual(shown["page_obj"].paginator.count, 54)
        self.assertEqual(shown["rows"][0]["weak_label"], "Timing only")
        self.assertIn("finding=1", shown["rows"][0]["url"])
        self.assertIn("weak=1", shown["rows"][0]["url"])
        self.assertIn("weak=1", shown["next_url"])

    def test_accepted_timing_only_findings_still_require_reveal(self):
        result = self.publish([timing_item()])
        self.review(result, "accepted")
        hidden = catalogue_page(self.case, QueryDict())
        self.assertEqual(hidden["page_obj"].paginator.count, 0)
        self.assertEqual(hidden["timing_only_count"], 1)
        shown = catalogue_page(self.case, QueryDict("weak=1"))
        self.assertEqual(shown["rows"][0]["item_index"], 0)

    def test_hidden_count_uses_search_filters_and_toggle_resets_only_finding_page(self):
        self.publish([timing_item(), timing_item(shared_words=["harbour"])])
        page = catalogue_page(
            self.case,
            QueryDict("method=conversation_link&q=adjacency&page=4&history=all"),
        )
        self.assertEqual(page["timing_only_count"], 1)
        query = parse_qs(urlsplit(page["timing_visibility_url"]).query)
        self.assertEqual(
            query,
            {
                "method": ["conversation_link"],
                "q": ["adjacency"],
                "history": ["all"],
                "weak": ["1"],
            },
        )
        unmatched = catalogue_page(self.case, QueryDict("q=missing"))
        self.assertEqual(unmatched["timing_only_count"], 0)
        shown = catalogue_page(self.case, QueryDict("weak=1&page=2"))
        self.assertNotIn(
            "weak", parse_qs(urlsplit(shown["timing_visibility_url"]).query)
        )
        self.assertNotIn(
            "page", parse_qs(urlsplit(shown["timing_visibility_url"]).query)
        )

    def test_finding_links_preserve_the_selected_visibility(self):
        self.publish([timing_item(), timing_item(shared_words=["harbour"])])
        default = catalogue_page(self.case, QueryDict())
        self.assertEqual(len(default["rows"]), 1)
        for row in default["rows"]:
            self.assertNotIn("weak", parse_qs(urlsplit(row["url"]).query))
        shown = catalogue_page(self.case, QueryDict("weak=1"))
        for row in shown["rows"]:
            self.assertEqual(parse_qs(urlsplit(row["url"]).query)["weak"], ["1"])

    def test_all_hidden_collection_remains_inspectable_for_each_case_role(self):
        self.publish([timing_item()])
        for role in ("owner", "editor", "viewer"):
            self.login(role)
            response = self.client.get(self.catalogue_url)
            self.assertContains(response, "1 timing-only finding hidden")
            self.assertContains(response, "Show all findings")
            self.assertContains(response, "No other findings match this selection")
            shown = self.client.get(self.catalogue_url, {"weak": "1"})
            self.assertContains(shown, "Recorded temporal adjacency")
            self.assertContains(shown, "Timing only")
            self.assertNotContains(shown, "Weak plate region")
        CaseMembership.objects.filter(
            case=self.case, user=self.users["viewer"]
        ).delete()
        self.assertEqual(
            self.client.get(self.catalogue_url, {"weak": "1"}).status_code, 404
        )
        self.login("outsider")
        self.assertEqual(
            self.client.get(self.catalogue_url, {"weak": "1"}).status_code, 404
        )

    def test_related_person_findings_offer_the_same_reveal_state(self):
        self.publish([timing_item()])
        self.login("owner")
        url = reverse("people:detail", args=[self.case.pk, self.person.pk])
        hidden = self.client.get(url, {"evidence_page": 2})
        self.assertEqual(hidden.context["related_findings"]["timing_only_count"], 1)
        self.assertContains(hidden, "Show all findings")
        shown = self.client.get(url, {"weak": "1"})
        self.assertEqual(
            shown.context["related_findings"]["page_obj"].paginator.count, 1
        )
        self.assertIn("weak=1", shown.context["related_findings_url"])

    def test_plate_catalogue_visibility_is_unchanged(self):
        self.publish(
            [
                {
                    "label": "Plate",
                    "text": "ABC123",
                    "values": {"Weak plate region": True},
                }
            ],
            operation="plate_ocr",
        )
        page = catalogue_page(self.case, QueryDict())
        self.assertEqual(page["page_obj"].paginator.count, 1)
        self.assertEqual(page["timing_only_count"], 0)
        self.assertEqual(page["rows"][0]["weak_label"], "Weak plate region")

    def test_migration_backfills_in_batches_reversibly_without_replacing_retained_data(
        self,
    ):
        items = [timing_item() for _ in range(501)]
        items += [
            timing_item(shared_words=["harbour"]),
            timing_item(status="unresolved"),
            timing_item(**{"Weak plate region": True}),
        ]
        result = self.publish(items)
        review = self.review(result, "accepted")
        ordinary = self.publish([timing_item()], operation="sessions")
        original = deepcopy(result.data)
        sources = list(
            AnalysisFindingSource.objects.filter(finding__result=result)
            .order_by("pk")
            .values()
        )
        finding_ids = list(
            result.findings.order_by("item_index").values_list("pk", flat=True)
        )
        result.findings.update(weak=False)
        migration = importlib.import_module("core.migrations.0006_timing_only_findings")
        schema_editor = SimpleNamespace(connection=connection)
        with patch(
            "django.core.files.storage.FileSystemStorage.open",
            side_effect=AssertionError("No source reads"),
        ):
            migration.forwards(apps, schema_editor)
            migration.forwards(apps, schema_editor)
        self.assertEqual(
            list(result.findings.order_by("item_index").values_list("weak", flat=True)),
            [True] * 501 + [False, False, True],
        )
        self.assertEqual(
            list(result.findings.order_by("item_index").values_list("pk", flat=True)),
            finding_ids,
        )
        self.assertFalse(ordinary.findings.get().weak)
        result.refresh_from_db()
        self.assertEqual(result.data, original)
        self.assertEqual(
            list(
                AnalysisFindingSource.objects.filter(finding__result=result)
                .order_by("pk")
                .values()
            ),
            sources,
        )
        self.assertEqual(
            AnalysisReview.objects.get(pk=review.pk).comment, "Synthetic review reason"
        )
        migration.backwards(apps, schema_editor)
        self.assertEqual(
            list(result.findings.order_by("item_index").values_list("weak", flat=True)),
            [False] * 503 + [True],
        )
        migration.forwards(apps, schema_editor)
        self.assertEqual(result.findings.filter(weak=True).count(), 502)
