"""Bounded extraction excerpts stay tied to the exact validated source."""

from unittest.mock import patch
from uuid import uuid4

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from audio.models import AudioEvidence
from core.analysis.catalogue import project_result
from core.analysis.navigation import finding_url
from core.analysis.search import derived_text_matches
from core.analysis.services import resolve_active_inputs, start_run
from core.analysis.sources import output
from core.models import AnalysisResult, ExtractedPassage
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from phone.models import CallEvidenceAttachment, PhoneCallEvidence


class ExtractedSearchTests(EvidenceViewTestMixin, TestCase):
    def document(self):
        return DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Needle in source metadata",
            file=SimpleUploadedFile("source.txt", b"Synthetic source text"),
        )

    def published(self, documents):
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "native_text",
            [{"kind": "document", "id": str(item.pk)} for item in documents],
        )
        run.status = "complete"
        run.save(update_fields=["status"])
        result = AnalysisResult.objects.create(
            case=self.case, run=run, data=output("Synthetic retained extraction")
        )
        for index, item in enumerate(documents):
            self.passage(result, item, index=index)
        return result

    def passage(self, result, item, *, index=0, **extra):
        return ExtractedPassage.objects.create(
            case=self.case,
            result=result,
            kind="document",
            evidence_id=item.pk,
            evidence_sha256=item.sha256,
            item_index=index,
            text="Synthetic needle in extracted content",
            **extra,
        )

    def rows(self, documents):
        return [
            {"kind": "document", "id": item.pk, "sha256": item.sha256}
            for item in documents
        ]

    def matches(self, documents):
        return derived_text_matches(
            case=self.case, rows=self.rows(documents), query="needle"
        )

    def test_candidate_query_and_shared_run_validation_do_not_repeat_per_row(self):
        documents = [self.document() for _ in range(5)]
        self.published(documents)
        for selection in (documents[:1], documents):
            with (
                self.subTest(rows=len(selection)),
                patch(
                    "core.analysis.search.resolve_active_inputs",
                    wraps=resolve_active_inputs,
                ) as resolve,
                CaptureQueriesContext(connection) as queries,
            ):
                matches = self.matches(selection)
            self.assertEqual(len(matches), len(selection))
            self.assertEqual(resolve.call_count, 1)
            # One bounded passage query, one size preflight and one original lookup.
            self.assertEqual(len(queries), 3)
            candidate_sql = queries[0]["sql"]
            self.assertNotIn(f'"{AnalysisResult._meta.db_table}"."data"', candidate_sql)
            self.assertNotIn(
                f'"{AnalysisResult._meta.db_table}"."provenance"', candidate_sql
            )
            self.assertNotIn(
                f'"{ExtractedPassage._meta.db_table}"."anchors"', candidate_sql
            )

    def test_newest_unavailable_run_falls_back_to_an_older_valid_version(self):
        original, missing = self.document(), self.document()
        older = self.published([original])
        self.published([original, missing])
        missing.file.storage.delete(missing.file.name)
        matches = self.matches([original])
        self.assertEqual(
            matches[("document", original.pk)]["url"],
            finding_url(self.case.pk, older.run_id, 0),
        )

    def test_ten_candidate_limit_is_per_evidence_and_failed_runs_are_checked_once(self):
        original, missing, other = (self.document() for _ in range(3))
        self.published([original, other])
        newest = self.published([original, missing])
        for index in range(1, 10):
            self.passage(newest, original, index=index)
        missing.file.storage.delete(missing.file.name)
        with patch(
            "core.analysis.search.resolve_active_inputs", wraps=resolve_active_inputs
        ) as resolve:
            matches = self.matches([original, other])
        self.assertNotIn(("document", original.pk), matches)
        self.assertIn(("document", other.pk), matches)
        self.assertEqual(resolve.call_count, 2)

    def test_stale_checksum_and_misattributed_source_are_not_displayed(self):
        original, unrelated = self.document(), self.document()
        result = self.published([original])
        self.passage(result, unrelated)
        ExtractedPassage.objects.filter(result=result, evidence_id=original.pk).update(
            evidence_sha256="f" * 64
        )
        self.assertEqual(self.matches([original, unrelated]), {})
        self.login("viewer")
        response = self.client.get(self.url("list"), {"q": "needle"})
        self.assertEqual(len(response.context["rows"]), 2)  # Metadata still matches.
        self.assertTrue(
            all(row["content_match"] is None for row in response.context["rows"])
        )

    def test_unprojected_archived_transcription_input_is_excluded_before_pagination(
        self,
    ):
        def recording(name):
            return AudioEvidence.objects.create(
                case=self.case,
                uploaded_by=self.users["owner"],
                title=name,
                file=SimpleUploadedFile(f"{name}.wav", b"Synthetic audio"),
            )

        original, peer = recording("recorder"), recording("second-recorder")
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "transcription",
            [{"kind": "audio", "id": str(item.pk)} for item in (original, peer)],
        )
        run.status = "complete"
        run.save(update_fields=["status"])
        phrase = "Only in the transcript"
        result = AnalysisResult.objects.create(
            case=self.case,
            run=run,
            data=output(
                "Synthetic transcription",
                items=[
                    {
                        "label": "Coarse transcript candidate",
                        "text": phrase,
                        "source_ids": [run.inputs[0]["id"]],
                    }
                ],
            ),
        )
        project_result(result)
        ExtractedPassage.objects.create(
            case=self.case,
            result=result,
            kind="audio",
            evidence_id=original.pk,
            evidence_sha256=original.sha256,
            text=phrase,
        )
        self.login("viewer")
        url = self.url("list")
        response = self.client.get(url, {"q": phrase})
        self.assertEqual(response.context["page_obj"].paginator.count, 1)
        self.assertEqual(response.context["rows"][0]["id"], original.pk)
        self.assertIn(phrase, response.context["rows"][0]["content_match"]["excerpt"])

        AudioEvidence.objects.filter(pk=peer.pk).update(
            is_archived=True,
            archive_changed_at=timezone.now(),
            archive_changed_by=self.users["owner"],
        )
        response = self.client.get(url, {"q": phrase})
        self.assertEqual(response.context["page_obj"].paginator.count, 0)
        self.assertEqual(response.context["rows"], [])

    def test_passage_attachment_must_match_the_validated_call_source(self):
        call = PhoneCallEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic call",
            sender=self.person,
            recipient=self.person,
        )
        attachment = CallEvidenceAttachment.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            phone_call=call,
            transcript="Supplied source material",
        )
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "transcription",
            [
                {
                    "kind": "phone-call",
                    "id": str(call.pk),
                    "attachment_id": str(attachment.pk),
                }
            ],
        )
        run.status = "complete"
        run.save(update_fields=["status"])
        result = AnalysisResult.objects.create(
            case=self.case, run=run, data=output("Synthetic transcription")
        )
        passage = ExtractedPassage.objects.create(
            case=self.case,
            result=result,
            kind="phone-call",
            evidence_id=call.pk,
            evidence_sha256=call.sha256,
            attachment_id=uuid4(),
            text="Needle in derived transcript",
        )
        rows = [{"kind": "phone-call", "id": call.pk, "sha256": call.sha256}]
        self.assertEqual(
            derived_text_matches(case=self.case, rows=rows, query="needle"), {}
        )
        ExtractedPassage.objects.filter(pk=passage.pk).update(
            attachment_id=attachment.pk
        )
        self.assertIn(
            ("phone-call", call.pk),
            derived_text_matches(case=self.case, rows=rows, query="needle"),
        )

    def test_excerpts_are_bounded_case_insensitive_and_escaped(self):
        original = self.document()
        result = self.published([original])
        ExtractedPassage.objects.filter(result=result).update(
            text="x" * 500 + " Café NEEDLE <script>untrusted</script> " + "z" * 1000
        )
        self.login("viewer")
        response = self.client.get(self.url("list"), {"q": "café needle"})
        excerpt = response.context["rows"][0]["content_match"]["excerpt"]
        self.assertLessEqual(len(excerpt), 300)
        self.assertIn("Café NEEDLE", excerpt)
        self.assertContains(response, "&lt;script&gt;untrusted&lt;/script&gt;")
        self.assertNotContains(response, "<script>untrusted</script>")

    def test_empty_page_or_query_does_not_query(self):
        with self.assertNumQueries(0):
            self.assertEqual(
                derived_text_matches(case=self.case, rows=[], query="needle"), {}
            )
            self.assertEqual(
                derived_text_matches(case=self.case, rows=[{}], query=""), {}
            )
