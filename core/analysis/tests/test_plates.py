"""Case-authorized plate processing, private crops, and publication integrity."""

from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.core.exceptions import PermissionDenied
from django.core.files.base import ContentFile
from django.http import Http404
from django.test import TestCase

from cases.models import CaseMembership
from core.analysis.artifacts import decode_artifact
from core.analysis.errors import AnalysisError
from core.analysis.forms import analysis_form
from core.analysis.services import process_run, start_run
from core.analysis.sources import output
from core.analysis.testing import local_ocr_response
from core.models import (
    AnalysisArtifact,
    AnalysisResult,
    ExtractedPassage,
    GatewayRequest,
)
from core.testing import EvidenceViewTestMixin
from image.models import ImageEvidence
from image.testing import plate_scene


class PlateIntegrationTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.original = plate_scene()
        self.image = ImageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic plate scene",
            file=ContentFile(self.original, name="synthetic-plate.jpeg"),
        )

    def start(self, role="owner"):
        return start_run(
            self.users[role],
            self.case.pk,
            "plate_ocr",
            [{"kind": "image", "id": str(self.image.pk)}],
        )[0]

    def test_local_form_only_selects_one_original_and_never_requests_cloud_approval(
        self,
    ):
        form = analysis_form(
            {
                "operation": "plate_ocr",
                "submission_id": uuid4(),
                "sources": [f"image:{self.image.pk}"],
                "external_processing": True,
            },
            case=self.case,
            operation="plate_ocr",
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertFalse(form.cleaned_data["external_processing"])
        self.assertNotIn("backend", form.fields)
        self.assertEqual(form.parameters(), {})
        for role in ("viewer", "outsider"):
            with (
                self.subTest(role=role),
                self.assertRaises((PermissionDenied, Http404)),
            ):
                self.start(role)
        run = self.start("editor")
        self.assertFalse(run.external_processing)

    def test_real_processing_keeps_exact_crops_search_and_current_private_access(self):
        run = self.start()
        with patch("core.analysis.gateway.GatewayClient.request") as gateway:
            process_run(run.pk)
        gateway.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.status, "partial", run.error_code)
        self.assertFalse(GatewayRequest.objects.filter(run=run).exists())
        passages = list(
            ExtractedPassage.objects.filter(result=run.result).values_list(
                "text", flat=True
            )
        )
        self.assertCountEqual(
            passages,
            [item["text"] for item in run.result.data["items"] if item["text"]],
        )
        self.assertTrue(any("test 482" in text.casefold() for text in passages))
        crops = list(run.result.artifacts.exclude(metadata={}))
        self.assertEqual(len(crops), 3)
        for crop in crops:
            finding = run.result.data["items"][crop.metadata["finding_index"]]
            self.assertEqual(crop.sha256, finding["values"]["Recognition JPEG SHA-256"])
        crop = crops[0]
        from django.urls import reverse

        url = reverse("core:analysis:artifact", args=[self.case.pk, run.pk, crop.pk])
        detail_url = reverse("core:analysis:detail", args=[self.case.pk, run.pk])
        for role, expected in (
            ("owner", 200),
            ("editor", 200),
            ("viewer", 200),
            ("outsider", 404),
        ):
            self.login(role)
            response = self.client.get(url)
            self.assertEqual(response.status_code, expected)
            self.assertIn("private", response["Cache-Control"])
            if expected == 200:
                self.assertContains(
                    self.client.get(detail_url), "Processed plate-region crop"
                )
        self.login("viewer")
        self.assertContains(
            self.client.get(self.url("list"), {"q": "TEST 482"}),
            "Synthetic plate scene",
        )
        CaseMembership.objects.filter(
            case=self.case, user=self.users["viewer"]
        ).delete()
        self.assertEqual(self.client.get(url).status_code, 404)
        with self.image.file.open("rb") as original:
            self.assertEqual(original.read(), self.original)

    def test_revoked_editor_cannot_publish_a_queued_plate_run(self):
        run = self.start("editor")
        CaseMembership.objects.filter(
            case=self.case, user=self.users["editor"]
        ).delete()
        with patch("image.analysis.plates.run_parser") as parser:
            process_run(run.pk)
        parser.assert_not_called()
        self.assertFalse(AnalysisResult.objects.filter(run=run).exists())

    def test_crop_cannot_point_at_a_missing_finding_and_publication_rolls_back(self):
        run = self.start()
        source_id = run.inputs[0]["id"]
        prepared = local_ocr_response()
        artifact = {
            "source_id": source_id,
            "content_type": "image/jpeg",
            "data_base64": prepared["image"],
            "width": prepared["prepared_width"],
            "height": prepared["prepared_height"],
            "metadata": {"finding_index": 1},
        }
        data = output(
            "Synthetic plate candidate",
            items=[
                {
                    "label": "Candidate",
                    "text": "TEST 482",
                    "source_ids": [source_id],
                    "anchors": [],
                }
            ],
            outcome="partial",
        ) | {"artifacts": [artifact]}
        with patch(
            "core.analysis.services.get_operation",
            return_value=SimpleNamespace(
                version=run.method_version,
                function=Mock(return_value=data),
            ),
        ):
            process_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.error_code, "invalid_artifact")
        self.assertFalse(AnalysisResult.objects.filter(run=run).exists())
        self.assertFalse(AnalysisArtifact.objects.filter(result__run=run).exists())
        for value in (True, -1, 10000, "0"):
            artifact["metadata"] = {"finding_index": value}
            with (
                self.subTest(value=value),
                self.assertRaisesMessage(AnalysisError, "invalid_artifact"),
            ):
                decode_artifact(artifact, {source_id})
