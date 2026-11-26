"""Meaning search recovery, approval checkpoints, and mixed-width publication."""

import hashlib
from io import StringIO
from unittest.mock import patch
from uuid import UUID

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from core.analysis.errors import AnalysisError
from core.analysis.source_selection import snapshot_sources
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
from core.models import (
    AnalysisResult,
    AnalysisRun,
    CaseSearchIndex,
    ExtractedPassage,
    SearchDocument,
    SearchPassage,
)
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
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

    def test_archived_sources_skip_backfill_and_resume_after_restore(self):
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
        self.assertFalse(SearchDocument.objects.filter(text=archived).exists())
        archived_doc = SearchDocument.objects.create(case=self.case, text=archived)

        def revoke(texts, authorize, **kwargs):
            self.assertEqual(texts, [added.body])
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
        self.assertEqual((state["documents"], state["passages"]), (2, 2))
        self.assertFalse(SearchPassage.objects.filter(document=archived_doc).exists())

        self.login("owner")
        self.assertEqual(
            self.client.post(self.url("restore", archived)).status_code, 302
        )
        self.seed()
        with patch(
            "core.case_search.indexing.embed", side_effect=self.hosted_vectors
        ) as embed:
            self.assertEqual(self.step(), "queued")
            self.assertEqual(self.step(), "ready")
            self.assertEqual(embed.call_args.args[0], [archived.body])
        state = search_index_status(self.case)
        self.assertEqual((state["documents"], state["passages"]), (3, 3))

    def test_archiving_before_transfer_supersedes_the_embedding_delivery(self):
        self.approve()
        item = self.source()
        self.seed()
        self.login("owner")

        def archive(texts, authorize, **kwargs):
            self.assertEqual(texts, [item.body])
            self.assertEqual(
                self.client.post(self.url("archive", item)).status_code, 302
            )
            return self.hosted_vectors(texts, authorize, **kwargs)

        with patch("core.case_search.indexing.embed", side_effect=archive) as embed:
            self.assertEqual(self.step(), "superseded")
            self.assertEqual(self.step(), "ready")
            embed.assert_called_once()
        self.assertFalse(SearchPassage.objects.filter(case=self.case).exists())

    def test_restore_restarts_partial_index_for_unprojected_archived_dependency(self):
        self.approve()
        active, archived = [
            DocumentEvidence.objects.create(
                case=self.case,
                uploaded_by=self.case.owner,
                title=title,
                file=SimpleUploadedFile("source.txt", title.encode()),
            )
            for title in ("Primary document", "Another document")
        ]
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.case.owner,
            operation="native_text",
            method_version="1",
            inputs=snapshot_sources(
                self.case,
                [{"kind": "document", "id": item.pk} for item in (active, archived)],
            ),
            status="complete",
        )
        result = AnalysisResult.objects.create(case=self.case, run=run)
        text = "Synthetic extract"
        passage = ExtractedPassage.objects.create(
            case=self.case,
            result=result,
            kind="document",
            evidence_id=active.pk,
            evidence_sha256=active.sha256,
            text=text,
            item_index=0,
        )
        document = SearchDocument.objects.create(
            case=self.case, extraction=passage, offset=1, profile=profile()
        )
        SearchPassage.objects.create(
            case=self.case,
            document=document,
            start=0,
            length=1,
            profile=profile(),
            digest=hashlib.sha256(text[:1].encode()).hexdigest(),
            embedding=[1.0] + [0.0] * 1535,
        )
        self.login("owner")
        self.assertEqual(
            self.client.post(
                self.url("archive", archived, kind="document")
            ).status_code,
            302,
        )
        self.seed()
        with patch(
            "core.case_search.indexing.embed", side_effect=self.hosted_vectors
        ) as embed:
            self.assertEqual(self.step(), "queued")
            self.assertEqual(self.step(), "ready")
            embed.assert_not_called()
        document.refresh_from_db()
        self.assertEqual(
            (document.profile, document.offset, document.complete), ("", 0, True)
        )
        self.assertFalse(SearchPassage.objects.filter(document=document).exists())

        self.assertEqual(
            self.client.post(
                self.url("restore", archived, kind="document")
            ).status_code,
            302,
        )
        self.seed()
        with patch(
            "core.case_search.indexing.embed", side_effect=self.hosted_vectors
        ) as embed:
            self.assertEqual(self.step(), "queued")
            self.assertEqual(self.step(), "ready")
            self.assertEqual(embed.call_args.args[0], [text])
        document.refresh_from_db()
        self.assertTrue(document.complete)
        self.assertEqual(document.offset, len(text))
        self.assertEqual(SearchPassage.objects.get(document=document).start, 0)

    def test_unreadable_active_original_can_resume_after_file_recovery(self):
        self.approve()
        original = b"Synthetic original"
        item = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.case.owner,
            title="Document",
            file=SimpleUploadedFile("source.txt", original),
        )
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.case.owner,
            operation="native_text",
            method_version="1",
            inputs=snapshot_sources(self.case, [{"kind": "document", "id": item.pk}]),
            status="complete",
        )
        result = AnalysisResult.objects.create(case=self.case, run=run)
        text = "Retained synthetic extraction"
        passage = ExtractedPassage.objects.create(
            case=self.case,
            result=result,
            kind="document",
            evidence_id=item.pk,
            evidence_sha256=item.sha256,
            text=text,
            item_index=0,
        )
        item.file.storage.delete(item.file.name)
        self.seed()
        document = SearchDocument.objects.get(extraction=passage)
        SearchDocument.objects.filter(pk=document.pk).update(id=UUID(int=1))
        document.pk = UUID(int=1)
        healthy = self.source("Later healthy source")
        healthy_doc = SearchDocument.objects.get(text=healthy)
        with (
            patch("core.case_search.indexing.BATCH_SIZE", 1),
            patch(
                "core.case_search.indexing.embed", side_effect=self.hosted_vectors
            ) as embed,
        ):
            self.assertEqual(self.step(), "queued")
            self.index.refresh_from_db()
            self.assertEqual(self.index.failures, 0)
            embed.assert_not_called()

            self.assertEqual(self.step(), "queued")
            healthy_doc.refresh_from_db()
            self.assertTrue(healthy_doc.complete)
            self.assertEqual(embed.call_args.args[0], [healthy.body])
            self.assertEqual(search_index_status(self.case)["documents"], 1)

            for delay in (15, 60):
                self.assertEqual(self.step(), "queued")
                queue = DBTaskResult.objects.latest("enqueued_at")
                self.assertAlmostEqual(
                    (queue.run_after - queue.enqueued_at).total_seconds(),
                    delay,
                    delta=2,
                )
            self.assertEqual(self.step(), "failed")
            embed.assert_called_once()
        document.refresh_from_db()
        self.assertFalse(document.complete)
        self.index.refresh_from_db()
        self.assertEqual(self.index.error_code, "source_unavailable")
        self.assertTrue(search_index_status(self.case)["can_retry"])

        self.assertEqual(
            item.file.storage.save(item.file.name, ContentFile(original)),
            item.file.name,
        )
        self.assertTrue(prepare_case_search(self.case, self.case.owner, retry=True))
        with patch(
            "core.case_search.indexing.embed", side_effect=self.hosted_vectors
        ) as embed:
            self.assertEqual(self.step(), "queued")
            self.assertEqual(self.step(), "ready")
            self.assertEqual(embed.call_args.args[0], [text])
        document.refresh_from_db()
        self.assertTrue(document.complete)
        self.assertEqual(document.offset, len(text))
        self.assertFalse(prepare_case_search(self.case, self.case.owner, retry=True))

        # Older workers could mark an unreadable extraction complete at offset 0.
        SearchPassage.objects.filter(document=document).delete()
        SearchDocument.objects.filter(pk=document.pk).update(offset=0)
        self.assertTrue(prepare_case_search(self.case, self.case.owner))
        document.refresh_from_db()
        self.assertFalse(document.complete)
        with patch(
            "core.case_search.indexing.embed", side_effect=self.hosted_vectors
        ) as embed:
            self.assertEqual(self.step(), "queued")
            self.assertEqual(self.step(), "ready")
            self.assertEqual(embed.call_args.args[0], [text])

    def test_removal_during_retry_clears_the_prior_failure(self):
        self.approve()
        item = self.source()
        self.seed()
        with patch(
            "core.case_search.indexing.embed",
            side_effect=AnalysisError("gateway_unavailable"),
        ):
            self.assertEqual(self.step(), "queued")
        item.delete()
        self.assertEqual(self.step(), "ready")
        self.index.refresh_from_db()
        self.assertEqual((self.index.error_code, self.index.failures), ("", 0))

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
