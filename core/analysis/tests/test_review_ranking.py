"""Review order uses distinct, currently inspectable evidence before pagination."""

from importlib import import_module
from types import SimpleNamespace
from unittest.mock import patch

from django.apps import apps
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from core.analysis.catalogue import finding_queryset
from core.analysis.ranking import rank_findings
from core.analysis.review_signals import REVIEW_SIGNALS
from core.analysis.source_selection import snapshot_sources
from core.analysis.tests import test_catalogue
from core.models import AnalysisFinding
from core.tests.testing import EvidenceViewTestMixin
from text.models import TextEvidence


class ReviewRankingTests(EvidenceViewTestMixin, TestCase):
    publish = test_catalogue.CatalogueTests.publish

    def setUp(self):
        super().setUp()
        self.note = self.original("First original")
        self.key = "R-14451"

    def original(self, body):
        return TextEvidence.objects.create(
            case=self.case, uploaded_by=self.users["owner"], title="Source", body=body
        )

    def publish_items(self, original, labels, **kwargs):
        sources = snapshot_sources(self.case, [{"kind": "text", "id": original.pk}])
        return self.publish(
            sources=sources,
            items=[
                {"source_ids": [sources[0]["id"]], "label": label, "text": text}
                for label, text in labels
            ],
            **kwargs,
        )

    def ranked(self):
        scope = finding_queryset(self.case, {"evidence": self.note.pk, "kind": "text"})
        return list(
            rank_findings(scope, self.case).values_list(
                "item_index", "review_reason", "repeat_position"
            )
        )

    def test_distinct_matches_move_before_cutoff_and_repeats_follow_other_observations(
        self,
    ):
        labels = [("Detail", str(n)) for n in range(35)]
        labels += [("Identifier candidate", self.key)] * 2
        retained = self.publish_items(self.note, labels)
        peer = self.original("Second original")
        self.publish_items(peer, [("Identifier candidate", self.key)])
        ranked = self.ranked()
        self.assertEqual(ranked[0], (35, REVIEW_SIGNALS["matched_identifier"], 1))
        self.assertEqual(ranked[1], (0, "", 1))
        self.assertEqual(ranked[-1], (36, "", 2))
        self.assertEqual(retained.data["items"][0]["label"], "Detail")
        TextEvidence.objects.filter(pk=peer.pk).update(
            is_archived=True,
            archive_changed_at=timezone.now(),
            archive_changed_by=self.users["owner"],
        )
        self.assertNotIn(
            REVIEW_SIGNALS["matched_identifier"],
            [reason for _, reason, _ in self.ranked()],
        )
        TextEvidence.objects.filter(pk=peer.pk).update(
            is_archived=False, body="Changed"
        )
        self.assertNotIn(
            REVIEW_SIGNALS["matched_identifier"],
            [reason for _, reason, _ in self.ranked()],
        )

    def test_copies_reruns_common_values_and_superseded_matches_do_not_promote(self):
        items = [("Identifier candidate", self.key)]
        self.publish_items(self.note, items)
        self.publish_items(self.note, items, parameters={"alternate": True})
        copy = self.original(self.note.body)
        self.publish_items(copy, items)
        self.assertNotIn(
            REVIEW_SIGNALS["matched_identifier"],
            [reason for _, reason, _ in self.ranked()],
        )
        peer = self.original("Independent bytes")
        self.publish_items(peer, items)
        self.publish_items(peer, [], status="empty")
        self.assertNotIn(
            REVIEW_SIGNALS["matched_identifier"],
            [reason for _, reason, _ in self.ranked()],
        )
        for n in range(5):
            self.publish_items(self.original(f"Signature footer {n}"), items)
        self.assertNotIn(
            REVIEW_SIGNALS["matched_identifier"],
            [reason for _, reason, _ in self.ranked()],
        )

    def test_identifier_lookup_budget_preserves_every_item_and_favors_specific_matches(
        self,
    ):
        first = ("Identifier candidate", self.key)
        second = ("Identifier candidate", "B-11111")
        self.publish_items(self.note, [second, first])
        self.publish_items(self.original("Other receipt"), [first, second])
        self.publish_items(self.original("Footer"), [second])
        with patch("core.analysis.ranking.MAX_MATCH_KEYS", 1):
            self.assertEqual(
                self.ranked(),
                [(1, REVIEW_SIGNALS["matched_identifier"], 1), (0, "", 1)],
            )

    def test_other_case_projections_cannot_promote_a_finding(self):
        items = [("Identifier candidate", self.key)]
        self.publish_items(self.note, items)
        current = self.case
        self.case = self.other_case
        self.publish_items(self.original("Outside evidence"), items)
        self.case = current
        self.assertEqual(self.ranked(), [(0, "", 1)])

    def test_migration_backfills_exactly_the_new_projection_and_keeps_indices(self):
        retained = self.publish_items(self.note, [("Detail", "one"), ("Detail", "two")])
        projections = AnalysisFinding.objects.filter(result=retained).order_by(
            "item_index"
        )
        saved = list(
            projections.values_list("pk", "item_index", "repeat_key", "review_signal")
        )
        projections.update(repeat_key="")
        migration = import_module("core.migrations.0010_backfill_finding_review")
        for _ in range(2):
            migration.backfill(apps, SimpleNamespace(connection=connection))
        self.assertEqual(
            list(
                projections.values_list(
                    "pk", "item_index", "repeat_key", "review_signal"
                )
            ),
            saved,
        )
