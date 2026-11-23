"""Meaning search recovery, approval checkpoints, and mixed-width publication."""

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from core.analysis.errors import AnalysisError
from core.case_search.approval import (
    approve_case_search,
    revoke_case_search,
    search_approval_context,
)
from core.case_search.indexing import (
    index_case_passages,
    prepare_case_search,
    search_index_status,
)
from core.case_search.semantic_model import profile
from core.case_search.tests.test_semantic import EMBEDDING, vectors
from core.models import CaseSearchIndex, SearchDocument, SearchPassage
from core.testing import EvidenceViewTestMixin
from text.models import TextEvidence


@override_settings(
    PANDECT_SEARCH_EMBEDDING_PROVIDER="openai-gateway",
    PANDECT_AI_GATEWAY={
        "account": "a" * 32,
        "gateway": "synthetic",
        "gateway_token": "synthetic-credential",
        "openai_key_alias": "default",
    },
)
class SearchRecoveryTests(EvidenceViewTestMixin, TestCase):
    def approve(self):
        owner = self.case.owner
        approve_case_search(self.case, owner, search_approval_context(self.case, owner))
        self.assertTrue(prepare_case_search(self.case, owner, retry=True))
        self.index = CaseSearchIndex.objects.get(case=self.case)

    def source(self, body="Synthetic interview.", **kwargs):
        return TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.case.owner,
            title="Interview",
            body=body,
            **kwargs,
        )

    def step(self):
        self.index.refresh_from_db()
        return index_case_passages.call(str(self.index.pk), str(self.index.token))

    def seed(self):
        for _ in range(5):
            self.assertEqual(self.step(), "queued")

    def hosted_vectors(self, texts, authorize, *, case, purpose):
        self.assertEqual(authorize().pk, self.case.pk)
        self.assertEqual(case.pk, self.case.pk)
        self.assertEqual(purpose, "index")
        return [[1.0] + [0.0] * 1535 for _ in texts]

    def test_retry_leaves_healthy_work_alone_and_recovers_only_confirmed_token(self):
        self.approve()
        for status in ("queued", "running"):
            CaseSearchIndex.objects.filter(pk=self.index.pk).update(status=status)
            self.assertFalse(search_index_status(self.case)["can_retry"])
            self.assertFalse(
                prepare_case_search(self.case, self.users["editor"], retry=True)
            )
            self.assertEqual(
                CaseSearchIndex.objects.get(pk=self.index.pk).token, self.index.token
            )
        output = StringIO()
        call_command(
            "prepare_case_search",
            "--case",
            str(self.case.pk),
            "--status",
            stdout=output,
        )
        self.assertIn(str(self.index.token), output.getvalue())
        args = ("--confirm-interrupted", str(self.index.token))
        with self.assertRaises(CommandError):
            call_command("prepare_case_search", *args, stdout=StringIO())
        call_command(
            "prepare_case_search", "--case", str(self.case.pk), *args, stdout=StringIO()
        )
        with self.assertRaises(CommandError):
            call_command(
                "prepare_case_search",
                "--case",
                str(self.case.pk),
                *args,
                stdout=StringIO(),
            )
        self.assertEqual(
            index_case_passages.call(str(self.index.pk), str(self.index.token)),
            "unavailable",
        )
        self.assertEqual(self.step(), "queued")

    def test_quota_stops_without_retry_and_rate_limit_delays_are_bounded(self):
        self.approve()
        self.source()
        self.seed()
        with patch(
            "core.case_search.indexing.embed",
            side_effect=AnalysisError("gateway_rate_limit"),
        ):
            for delay in (15, 60):
                self.assertEqual(self.step(), "queued")
                queue = DBTaskResult.objects.latest("enqueued_at")
                self.assertAlmostEqual(
                    (queue.run_after - queue.enqueued_at).total_seconds(),
                    delay,
                    delta=2,
                )
            self.assertEqual(self.step(), "failed")
        self.assertIn("busy", search_index_status(self.case)["reason"])
        self.assertTrue(prepare_case_search(self.case, self.case.owner, retry=True))
        with patch(
            "core.case_search.indexing.embed",
            side_effect=AnalysisError("gateway_quota"),
        ):
            self.assertEqual(self.step(), "failed")
        self.client.force_login(self.users["editor"])
        page = self.client.get(
            reverse("core:analysis:processing", args=(self.case.pk,))
        )
        self.assertContains(page, "no available quota")
        self.assertContains(page, "Resume indexing")
        self.assertContains(page, "0 completed sources")

    def test_archived_backfill_new_originals_and_revocation_during_request(self):
        archived = self.source(
            is_archived=True,
            archive_changed_at=timezone.now(),
            archive_changed_by=self.case.owner,
        )
        self.assertFalse(SearchDocument.objects.exists())
        self.assertFalse(prepare_case_search(self.case, self.case.owner, retry=True))
        self.approve()
        added = self.source()
        self.assertTrue(SearchDocument.objects.filter(text=added).exists())
        self.seed()
        self.assertTrue(SearchDocument.objects.filter(text=archived).exists())

        def revoke(texts, authorize, **kwargs):
            result = self.hosted_vectors(texts, authorize, **kwargs)
            revoke_case_search(self.case)
            return result

        with patch("core.case_search.indexing.embed", side_effect=revoke):
            self.assertEqual(self.step(), "superseded")
        self.assertFalse(SearchPassage.objects.exists())
        missed = self.source("Retained during pause")
        self.assertFalse(SearchDocument.objects.filter(text=missed).exists())
        self.approve()
        self.seed()
        with patch("core.case_search.indexing.embed", side_effect=self.hosted_vectors):
            self.assertEqual(self.step(), "queued")
            self.assertEqual(self.step(), "ready")
        state = search_index_status(self.case)
        self.assertEqual((state["documents"], state["passages"]), (3, 3))

    def test_rebuild_keeps_existing_vectors_until_long_source_is_complete(self):
        with override_settings(
            PANDECT_LOCAL_EMBEDDING=EMBEDDING, PANDECT_SEARCH_EMBEDDING_PROVIDER="local"
        ):
            retained = self.source("A delivery was rescheduled. " * 500)
            self.index = CaseSearchIndex.objects.get(case=self.case)
            self.seed()
            with patch("core.case_search.indexing.embed", side_effect=vectors):
                for _ in range(5):
                    if self.step() == "ready":
                        break
                else:
                    self.fail("The local index never completed.")
            previous = profile()
            old_passages = SearchPassage.objects.filter(
                document__text=retained, profile=previous
            )
            old_count = old_passages.count()
            self.assertGreater(old_count, 32)
        self.approve()
        self.seed()
        with patch("core.case_search.indexing.embed", side_effect=self.hosted_vectors):
            self.assertEqual(self.step(), "queued")
            self.assertEqual(old_passages.count(), old_count)
            state = search_index_status(self.case)
            self.assertEqual((state["documents"], state["passages"]), (0, 32))
            self.assertFalse(
                search_index_status(self.case, count_passages=False)["passages"]
            )
            self.client.force_login(self.users["viewer"])
            page = self.client.get(
                reverse("core:analysis:processing", args=(self.case.pk,))
            )
            self.assertContains(page, "0 completed sources · 32 indexed passages")
            output = StringIO()
            call_command(
                "prepare_case_search",
                "--case",
                str(self.case.pk),
                "--status",
                stdout=output,
            )
            self.assertIn("0 complete sources, 32 indexed passages", output.getvalue())
            for _ in range(5):
                if self.step() == "ready":
                    break
            else:
                self.fail("The document never finished rebuilding.")
        self.assertFalse(old_passages.exists())
        self.assertEqual(search_index_status(self.case)["passages"], old_count)
        with self.assertRaises(IntegrityError), transaction.atomic():
            SearchPassage.objects.filter(case=self.case).update(embedding=[1.0, 0.0])
