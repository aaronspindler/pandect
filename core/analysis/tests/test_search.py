"""Cloudflare hits for retained extraction still require exact current source inputs."""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone

from core.analysis.services import start_run
from core.analysis.sources import output
from core.case_search.tests.fixtures import indexed
from core.models import AnalysisResult, ExtractedPassage
from core.tests.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence


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

    def test_stale_multi_input_and_checksum_do_not_reopen_hosted_excerpts(self):
        first, peer = self.document(), self.document()
        result = self.published([first, peer])
        passage = result.passages.get(evidence_id=first.pk)
        self.login("viewer")
        sources = [("passage", "", passage.pk)]
        with indexed(self.case, sources):
            url = self.url("list")
            valid = self.client.get(url, {"q": "needle in extracted"})
            self.assertContains(valid, "Synthetic needle in extracted content")
            self.assertEqual([r["id"] for r in valid.context["rows"]], [first.pk])
            self.assertIn(
                str(result.run_id), valid.context["rows"][0]["content_match"]["url"]
            )
            DocumentEvidence.objects.filter(pk=peer.pk).update(
                is_archived=True,
                archive_changed_at=timezone.now(),
                archive_changed_by=self.users["owner"],
            )
            self.assertEqual(
                self.client.get(url, {"q": "needle in extracted"}).context["rows"], []
            )
            DocumentEvidence.objects.filter(pk=peer.pk).update(is_archived=False)
            ExtractedPassage.objects.filter(pk=passage.pk).update(
                evidence_sha256="e" * 64
            )
            self.assertEqual(
                self.client.get(url, {"q": "needle in extracted"}).context["rows"], []
            )
