from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

from django.http import Http404
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.analysis.bulk import prepare_case_analysis_batch
from core.analysis.source_selection import snapshot_sources
from core.case_search.grammar import parse_query
from core.case_search.parent_index import index_parent, prepare_parents, scan_parents
from core.case_search.parents import (
    VERSION,
    anchor,
    current_parent_ids,
    parent_results,
    read_anchors,
)
from core.case_search.queries import RelatedMatches
from core.case_search.retained import RetainedMatches
from core.models import (
    AnalysisResult,
    AnalysisRun,
    SearchDocument,
    SearchParent,
    SearchParentScan,
    SearchPassage,
)
from core.testing import EvidenceViewTestMixin
from phone.grouping_jobs import grouping_scope, start_grouping
from phone.models import ConversationSession, TextMessageEvidence
from text.models import TextEvidence


@override_settings(PANDECT_SEARCH_EMBEDDING_PROVIDER="disabled")
class SearchParentTests(EvidenceViewTestMixin, TestCase):
    def text(self, body, title="Pier dispatch"):
        return TextEvidence.objects.create(
            case=self.case, uploaded_by=self.users["owner"], title=title, body=body
        )

    def build(self, source, pk):
        index_parent.call(
            str(self.case.pk), str(self.users["owner"].pk), source, str(pk)
        )

    def search(self, question, **kwargs):
        return parent_results(
            self.case, user=self.users["viewer"], query=question, **kwargs
        )

    def test_local_outline_search_is_idempotent_and_resolves_exact_current_spans(self):
        body = "General notes. " * 120 + "The gondola crane was quarantined."
        record = self.text(body)
        self.build("text", record.pk)
        parents = SearchParent.objects.count()
        self.build("text", record.pk)
        self.assertEqual(SearchParent.objects.count(), parents)
        hits = self.search("quarantined")
        self.assertTrue(any("quarantined" in hit["excerpt"] for hit in hits))
        for hit in hits:
            self.assertEqual(hit["source"], ("text", record.pk))
            self.assertEqual(
                body[hit["start"] : hit["start"] + len(hit["excerpt"])], hit["excerpt"]
            )
        with self.assertRaises(Http404):
            parent_results(self.case, user=self.users["outsider"], query="quarantined")
        self.assertEqual(
            self.search(
                "quarantined", through=record.created_at - timedelta(seconds=1)
            ),
            [],
        )
        self.assertEqual(self.search("quarantined", record_type="image"), [])
        TextEvidence.objects.filter(pk=record.pk).update(
            is_archived=True,
            archive_changed_at=timezone.now(),
            archive_changed_by=self.users["owner"],
        )
        self.assertFalse(self.search("quarantined"))
        TextEvidence.objects.filter(pk=record.pk).update(
            is_archived=False,
            archive_changed_at=timezone.now(),
            archive_changed_by=self.users["owner"],
            body="replacement",
        )
        self.assertFalse(self.search("quarantined"))

    def test_outline_search_uses_the_current_title(self):
        record = self.text(
            "Synthetic exhibit from the recorder. " * 30, title="Lantern"
        )
        self.build("text", record.pk)
        self.assertTrue(self.search("Lantern", record_type="text"))
        self.assertFalse(
            SearchParent.objects.filter(
                text=record, outline__icontains="Lantern"
            ).exists()
        )
        SearchParent.objects.create(
            case=self.case,
            text=record,
            version=VERSION - 1,
            label="Document section",
            outline="Lantern",
            anchors=[anchor("text", record.pk, 0, record.body[:240])],
        )
        record.title = "Beacon"
        record.save(update_fields=["title"])
        self.assertFalse(self.search("Lantern", record_type="text"))
        self.assertTrue(self.search("Beacon", record_type="text"))
        self.assertTrue(self.search("exhibit", record_type="text"))
        self.assertFalse(self.search("Beacon", record_type="document"))

    def test_pooled_parent_vectors_return_child_for_semantic_only_question(self):
        record = self.text("General inventory of unopened boxes. " * 20)
        doc = SearchDocument.objects.create(
            case=self.case, text=record, complete=True, profile="synthetic"
        )
        SearchPassage.objects.create(
            case=self.case,
            document=doc,
            start=0,
            length=20,
            digest="0" * 64,
            profile="synthetic",
            embedding=[1.0, 0.0] + [0.0] * 382,
        )
        with patch("core.case_search.parent_index.profile", return_value="synthetic"):
            self.build("text", record.pk)
        for p in SearchParent.objects.filter(embedding__isnull=False):
            self.assertEqual(p.profile, "synthetic")
        self.assertFalse(self.search("freight"))
        self.assertTrue(
            self.search(
                "freight", vector=[1.0, 0.0] + [0.0] * 382, active_profile="synthetic"
            )
        )
        self.assertFalse(
            self.search(
                "freight",
                vector=[-1.0, 0.0] + [0.0] * 382,
                active_profile="synthetic",
                max_distance=0.6,
            )
        )

    def test_conversation_links_neighbor_and_disappears_on_changed_membership(self):
        messages = []
        for i, text in enumerate(
            ("Move the Mica pickup to Dock 7.", "Will do. The gate is locked.")
        ):
            messages.append(
                TextMessageEvidence.objects.create(
                    case=self.case,
                    uploaded_by=self.users["owner"],
                    title="Synthetic message",
                    sender=self.person,
                    recipient=self.person,
                    body=text,
                    occurred_at=datetime(2026, 1, 2, tzinfo=UTC) + timedelta(minutes=i),
                )
            )
        _, digest = grouping_scope(self.case, self.person, self.person)
        batch, _ = start_grouping(
            self.users["owner"],
            self.case,
            self.person,
            self.person,
            gap_minutes=60,
            reviewed_scope=digest,
            submission_id=uuid4(),
        )
        self.assertEqual(prepare_case_analysis_batch(batch.pk), "complete")
        session = ConversationSession.objects.get(grouping__batch=batch)
        self.build("session", session.pk)
        hits = self.search("Mica", record_type="conversation")
        self.assertEqual({hit["id"] for hit in hits}, {m.pk for m in messages})
        TextMessageEvidence.objects.filter(pk=messages[0].pk).update(
            is_archived=True,
            archive_changed_at=timezone.now(),
            archive_changed_by=self.users["owner"],
        )
        self.assertFalse(self.search("locked", record_type="conversation"))

    def _build_summary(self):
        messages = [
            TextMessageEvidence.objects.create(
                case=self.case,
                uploaded_by=self.users["owner"],
                title="Dispatch",
                sender=self.person,
                recipient=self.person,
                body=body,
            )
            for body in ("Move it to Dock 7.", "Another unrelated message.")
        ]
        inputs = snapshot_sources(
            self.case, [{"kind": "text-message", "id": m.pk} for m in messages]
        )
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.users["owner"],
            operation="session_summaries",
            method_version="test",
            inputs=inputs,
            status="partial",
        )
        target = next(i for i in inputs if i["evidence_id"] == str(messages[0].pk))
        result = AnalysisResult.objects.create(
            case=self.case,
            run=run,
            data={
                "items": [
                    {
                        "label": "Change",
                        "text": "The rendezvous for collection was amended.",
                        "source_ids": [target["id"]],
                        "anchors": [
                            {
                                "source_id": target["id"],
                                "start": 0,
                                "end": len(messages[0].body),
                            }
                        ],
                        "values": {"chunk_source_ids": [i["id"] for i in inputs]},
                    }
                ]
            },
        )
        self.build("summary", result.pk)
        return messages

    def test_canonical_conversation_summary_finds_only_the_cited_message(self):
        messages = self._build_summary()
        hits = self.search("rendezvous")
        self.assertEqual(
            [(r["id"], r["excerpt"], r["via"]) for r in hits],
            [(messages[0].pk, messages[0].body, "Conversation summary")],
        )
        self.assertIn("Partial coverage", hits[0]["badges"])
        TextMessageEvidence.objects.filter(pk=messages[1].pk).update(
            is_archived=True,
            archive_changed_at=timezone.now(),
            archive_changed_by=self.users["owner"],
        )
        self.assertFalse(self.search("rendezvous"))

    def test_type_date_and_exclusions_apply_before_lexical_and_vector_limits(self):
        messages = self._build_summary()
        decoy = self.text("Retired dispatch note")
        TextEvidence.objects.filter(pk=decoy.pk).update(
            collected_at=messages[0].created_at - timedelta(days=5)
        )
        vector = [1.0, 0.0]
        SearchParent.objects.filter(summary__isnull=False).update(
            embedding=[0.8, 0.2], profile="synthetic"
        )
        SearchParent.objects.bulk_create(
            [
                SearchParent(
                    case=self.case,
                    text=decoy,
                    version=VERSION,
                    label="Document section",
                    outline="rendezvous " * 15,
                    anchors=[anchor("text", decoy.pk, 0, decoy.body)],
                    embedding=vector,
                    profile="synthetic",
                )
                for _ in range(26)
            ]
        )
        parsed = parse_query("rendezvous -retired")
        terms = tuple(t for group in parsed.groups for t in group if t.negated)
        filters = [
            {"record_type": "text-message"},
            {"through": AnalysisResult.objects.get(case=self.case).created_at},
            {"start": messages[0].created_at - timedelta(days=1)},
            {
                "exclusions": terms,
                "related": RelatedMatches(self.case, RetainedMatches(parsed)),
            },
        ]
        for query, extra in [
            ("rendezvous", {}),
            ("cargo", {"vector": vector, "active_profile": "synthetic"}),
        ]:
            self.assertNotIn(
                messages[0].pk, [r["id"] for r in self.search(query, **extra)]
            )
            for scope in filters:
                with self.subTest(query=query, scope=tuple(scope)):
                    rows = self.search(query, **extra, **scope)
                    self.assertEqual(
                        [(r["id"], r["via"]) for r in rows],
                        [(messages[0].pk, "Conversation summary")],
                    )

    def test_document_brief_rejects_an_anchor_from_an_unrelated_original(self):
        record = self.text("Blue seal")
        decoy = self.text("Blue seal")
        inputs = snapshot_sources(self.case, [{"kind": "text", "id": record.pk}])
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.users["owner"],
            operation="text_topics",
            method_version="test",
            inputs=inputs,
            status="complete",
            parameters={"_search_generation": "briefs"},
        )
        citation = {
            "source_id": inputs[0]["id"],
            "search_start": 0,
            "quote": "Blue seal",
            "search_source": "text",
        }
        result = AnalysisResult.objects.create(
            case=self.case,
            run=run,
            data={
                "items": [
                    {
                        "text": "Cobalt envelope",
                        "anchors": [citation | {"search_source_id": str(record.pk)}],
                    },
                    {
                        "text": "Foreign packet",
                        "anchors": [citation | {"search_source_id": str(decoy.pk)}],
                    },
                ]
            },
        )
        self.build("summary", result.pk)
        hits = self.search("cobalt")
        self.assertEqual(
            [(r["id"], r["excerpt"], r["via"]) for r in hits],
            [(record.pk, "Blue seal", "Document brief")],
        )
        self.assertFalse(self.search("foreign"))

    def test_normal_archive_removes_parent_and_restore_schedules_only_its_sources(self):
        first = self.text("Exhibit anchor. " * 30)
        other = self.text("Another record. " * 30)
        self.build("text", first.pk)
        self.build("text", other.pk)
        leads = {r["via_id"] for r in self.search("exhibit")}
        self.assertTrue(leads)
        self.login("editor")
        args = (self.case.pk, "text", first.pk)
        self.assertEqual(
            self.client.post(reverse("core:evidence:archive", args=args)).status_code,
            302,
        )
        self.assertFalse(current_parent_ids(self.case, leads))
        self.assertTrue(SearchParent.objects.filter(text=other).exists())
        with patch("core.case_search.parent_index.index_children") as task:
            self.assertEqual(
                self.client.post(
                    reverse("core:evidence:restore", args=args)
                ).status_code,
                302,
            )
            task.enqueue.assert_any_call(
                str(self.case.pk),
                str(self.users["editor"].pk),
                "text",
                {"pk": str(first.pk)},
            )

    def test_confirmed_inventory_removes_only_dependent_outlines(
        self,
    ):
        first = self.text("Original synthetic exhibit. " * 40)
        independent = self.text("Independent material. " * 20)
        self.build("text", first.pk)
        self.build("text", independent.pk)
        ids = set(SearchParent.objects.filter(text=first).values_list("pk", flat=True))
        self.assertGreater(len(ids), 1)
        operator = self.users["owner"]
        operator.is_superuser = True
        operator.save(update_fields=["is_superuser"])
        self.login("owner")
        url = self.url("delete", first)
        review = self.client.get(url)
        self.assertContains(review, "Search outline", count=len(ids))
        confirm = {"revision": review.context["deletion_revision"], "confirm": "on"}
        self.assertEqual(self.client.post(url, confirm).status_code, 302)
        self.assertFalse(SearchParent.objects.filter(pk__in=ids).exists())
        self.assertTrue(SearchParent.objects.filter(text=independent).exists())

    def test_long_question_and_adjacent_anchors_use_bounded_original_reads(self):
        body = "A message about the quarantined exhibit. " * 32
        record = self.text(body)
        self.build("text", record.pk)
        hits = self.search(
            "What did the one who was there at the time when it was in the case "
            "write about the quarantined exhibit?"
        )
        self.assertTrue(any("quarantined" in hit["excerpt"] for hit in hits))
        refs = [
            anchor("text", record.pk, offset, body[offset : offset + 240])
            for offset in (0, 112, 240, 512, 722)
        ]
        with self.assertNumQueries(1):
            verified = read_anchors(self.case, refs, through=timezone.now())
        self.assertEqual(len(verified), len(refs))
        self.assertTrue(
            all(
                verified["text", str(record.pk), r["start"], r["digest"]]["excerpt"]
                == body[r["start"] : r["start"] + r["length"]]
                for r in refs
            )
        )

    def test_backfill_checkpoints_and_duplicate_scan_does_no_extra_work(self):
        self.text("Long record " * 100)
        self.assertTrue(prepare_parents(self.case, self.users["owner"]))
        self.assertFalse(prepare_parents(self.case, self.users["owner"]))
        scan = SearchParentScan.objects.get(case=self.case)
        with patch("core.case_search.parent_index.index_parent") as delivery:
            for _ in range(2):
                scan_parents.call(
                    str(scan.pk), str(self.users["owner"].pk), scan.version, 0, None
                )
            self.assertEqual(delivery.enqueue.call_count, 1)
        scan.refresh_from_db()
        self.assertEqual(scan.phase, 1)
