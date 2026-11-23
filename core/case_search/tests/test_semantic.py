"""The search worker publishes complete, current, source-linked local embeddings."""

import hashlib
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.http import Http404
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from cases.models import CaseMembership
from core.analysis.errors import AnalysisError
from core.analysis.services import start_run
from core.case_search.indexing import (
    index_case_passages,
    prepare_case_search,
    search_index_status,
)
from core.case_search.semantic import related_results
from core.case_search.semantic_model import CHUNK_BYTES, next_chunk
from core.models import (
    AnalysisResult,
    CaseSearchIndex,
    ExtractedPassage,
    SearchDocument,
    SearchPassage,
)
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from people.models import Person
from phone.models import TextMessageEvidence
from text.models import TextEvidence

EMBEDDING = {
    "url": "http://127.0.0.1:11434",
    "model": "all-minilm:22m",
    "runtime": "ollama@0.40.16",
    "sha256": "a" * 64,
}
UNIT = [1.0] + [0.0] * 383


def vectors(texts, authorize, **kwargs):
    authorize()
    return [UNIT for _ in texts]


class SplitTest(SimpleTestCase):
    def test_unicode_chunk_boundaries_round_trip(self):
        original = "é水😃 " * 190
        text = original
        chunks = []
        while text:
            chunk = next_chunk(text)
            self.assertLessEqual(len(chunk.encode()), CHUNK_BYTES)
            chunks.append(chunk)
            text = text[len(chunk) :]
        self.assertEqual("".join(chunks), original)


@override_settings(PANDECT_LOCAL_EMBEDDING=EMBEDDING)
class SemanticSearchTests(EvidenceViewTestMixin, TestCase):
    def source(self, body, case=None):
        case = case or self.case
        return TextEvidence.objects.create(
            case=case, uploaded_by=case.owner, title="Interview", body=body
        )

    def step(self):
        self.index.refresh_from_db()
        return index_case_passages.call(str(self.index.pk), str(self.index.token))

    def finish(self):
        self.index = CaseSearchIndex.objects.get(case=self.case)
        with patch("core.case_search.indexing.embed", side_effect=vectors):
            for _ in range(30):
                if self.step() == "ready":
                    return
        self.index.refresh_from_db()
        self.fail(
            f"Worker stopped with state {self.index.status}: {self.index.error_code}"
        )

    def hits(self, query="meeting cancelled", **filters):
        with patch("core.case_search.semantic.embed", side_effect=vectors):
            rows, _ = related_results(
                self.case, user=self.users["viewer"], query=query, **filters
            )
        return rows

    def test_old_and_new_sources_use_private_original_offsets_and_current_case(self):
        body = "We postponed the appointment. " * 20
        with override_settings(PANDECT_LOCAL_EMBEDDING={}):
            retained = self.source(body)
        self.assertTrue(prepare_case_search(self.case, self.users["owner"]))
        self.source("Other case's unrelated meeting", case=self.other_case)
        self.finish()
        passages = SearchPassage.objects.filter(document__text=retained).order_by(
            "start"
        )
        self.assertGreater(len(passages), 1)
        for passage in passages:
            segment = body[passage.start : passage.start + passage.length]
            self.assertEqual(
                passage.digest, hashlib.sha256(segment.encode()).hexdigest()
            )
        self.assertEqual([row["id"] for row in self.hits()], [retained.pk])
        self.assertFalse(self.hits("meeting -appointment"))
        self.assertFalse(self.hits(record_type="finding"))
        self.assertFalse(self.hits(query='"meeting"'))
        self.assertFalse(self.hits(query="ID-123"))
        self.source("Another appointment.")
        self.finish()
        self.assertEqual(len(self.hits()), 2)
        retained.delete()
        self.assertEqual(len(self.hits()), 1)
        self.assertEqual(SearchDocument.objects.filter(case=self.case).count(), 1)

    def test_one_long_source_cannot_crowd_out_another_related_record(self):
        long_source = self.source("The meeting moved. " * 1200)
        other = self.source("We postponed the appointment.")
        self.finish()
        self.assertGreater(
            SearchPassage.objects.filter(document__text=long_source).count(), 60
        )
        SearchPassage.objects.filter(document__text=other).update(
            embedding=[0.8, 0.6] + [0.0] * 382
        )
        first, second = self.hits()
        self.assertEqual((first["id"], second["id"]), (long_source.pk, other.pk))
        self.assertEqual(
            [row["id"] for row in self.hits(keyword_urls={first["url"]})], [other.pk]
        )

    def test_failed_batch_is_recoverable_without_duplicate_or_partial_publication(self):
        self.source("The appointment moved. " * 2500)
        self.index = CaseSearchIndex.objects.get(case=self.case)
        for _ in range(5):
            self.assertEqual(self.step(), "queued")
        with patch("core.case_search.indexing.embed", side_effect=vectors):
            self.assertEqual(self.step(), "queued")
        self.assertGreater(SearchPassage.objects.count(), 0)
        self.assertEqual(self.hits(), [])
        failure = AnalysisError("local_ai_unavailable")
        with patch("core.case_search.indexing.embed", side_effect=failure):
            self.assertEqual(
                [self.step() for _ in range(3)], ["queued", "queued", "failed"]
            )
        self.assertTrue(search_index_status(self.case)["can_retry"])
        self.assertTrue(
            prepare_case_search(self.case, self.users["editor"], retry=True)
        )
        self.finish()
        self.assertEqual(len(self.hits()), 1)

    def test_revoke_during_request_blocks_query_and_worker_publication(self):
        self.source("The meeting moved.")
        self.finish()

        def revoke(texts, authorize, **kwargs):
            CaseMembership.objects.filter(
                case=self.case, user=self.users["viewer"]
            ).delete()
            authorize()

        with (
            patch("core.case_search.semantic.embed", side_effect=revoke),
            self.assertRaises(Http404),
        ):
            related_results(
                self.case, user=self.users["viewer"], query="appointment moved"
            )
        self.index.requested_by = self.users["editor"]
        self.index.save(update_fields=["requested_by"])
        self.source("Another message")
        CaseMembership.objects.filter(
            case=self.case, user=self.users["editor"]
        ).delete()
        self.assertEqual(self.step(), "failed")
        self.assertEqual(SearchPassage.objects.count(), 1)

    def test_message_batch_query_count_does_not_scale_with_the_number_of_sources(self):
        counts = []
        for case, size in ((self.case, 1), (self.other_case, 32)):
            participant = Person.objects.create(case=case, first_name="Synthetic")
            messages = [
                TextMessageEvidence(
                    case=case,
                    uploaded_by=case.owner,
                    sender=participant,
                    recipient=participant,
                    title="Message",
                    body=f"Number {number}.",
                )
                for number in range(size)
            ]
            for message in messages:
                message._prepare_original()
            TextMessageEvidence.objects.bulk_create(messages)
            prepare_case_search(case, case.owner)
            self.index = CaseSearchIndex.objects.get(case=case)
            for _ in range(5):
                self.assertEqual(self.step(), "queued")
            with (
                patch("core.case_search.indexing.embed", side_effect=vectors),
                CaptureQueriesContext(connection) as queries,
            ):
                self.assertEqual(self.step(), "queued")
            counts.append(len(queries))
            self.assertEqual(SearchPassage.objects.filter(case=case).count(), size)
        self.assertLessEqual(counts[1], counts[0] + 1)

    def test_extraction_requires_current_file_and_reconstructed_source_identity(self):
        original = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Notes",
            file=SimpleUploadedFile("notes.txt", b"Synthetic meeting."),
        )
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "native_text",
            [{"kind": "document", "id": str(original.pk)}],
        )
        run.status = "complete"
        run.save(update_fields=["status"])
        result = AnalysisResult.objects.create(run=run, case=self.case)
        ExtractedPassage.objects.create(
            result=result,
            case=self.case,
            kind="document",
            evidence_sha256=original.sha256,
            evidence_id=original.pk,
            text="The appointment moved.",
            item_index=0,
        )
        self.assertTrue(prepare_case_search(self.case, self.users["editor"]))
        self.finish()
        rows = self.hits()
        self.assertEqual([r["id"] for r in rows], [original.pk])
        self.assertIn("#finding-1", rows[0]["url"])
        original.file.storage.delete(original.file.name)
        self.assertEqual(self.hits(), [])


@override_settings(PANDECT_LOCAL_EMBEDDING=EMBEDDING)
class EmbeddingProtocolTests(SimpleTestCase):
    def test_rejects_wrong_version_dimensions_nonfinite_and_nonlocal_models(self):
        from core.case_search.semantic_model import embed

        expected = "sha256:" + EMBEDDING["sha256"] + ":local"
        normal = {
            "/api/version": {"version": "0.40.16"},
            "/api/tags": {"models": [{"name": "all-minilm:22m", "digest": "a" * 64}]},
            "/api/show": {"details": {"family": "bert"}, "capabilities": ["embedding"]},
            "/api/embed": {"model": expected, "embeddings": [UNIT]},
        }

        def run(overrides):
            responses = normal | overrides
            with patch(
                "core.analysis.local.LocalClient._request",
                side_effect=lambda path, payload=None: responses[path],
            ) as request:
                result = embed(["Synthetic text."], lambda: None)
            payload = next(
                args[0][1]
                for args in request.call_args_list
                if args[0][0] == "/api/embed"
            )
            self.assertEqual(payload["model"], expected)
            self.assertFalse(payload["truncate"])
            return result

        self.assertEqual(run({}), [UNIT])
        for override in (
            {"/api/version": {"version": "0.40.17"}},
            {"/api/show": normal["/api/show"] | {"remote_model": "external"}},
            {"/api/embed": {"model": "different", "embeddings": [UNIT]}},
            {"/api/embed": {"model": expected, "embeddings": [[0.5]]}},
            {
                "/api/embed": {
                    "model": expected,
                    "embeddings": [[float("nan")] + UNIT[1:]],
                }
            },
            {"/api/embed": {"model": expected, "embeddings": [[0.0] * 384]}},
        ):
            with (
                self.subTest(override=list(override)),
                self.assertRaises(AnalysisError),
            ):
                run(override)
