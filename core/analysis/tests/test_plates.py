"""Case-authorized plate processing, private crops, and publication integrity."""

from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.core.exceptions import PermissionDenied
from django.core.files.base import ContentFile
from django.http import Http404
from django.test import TestCase
from django.urls import reverse

from cases.models import CaseMembership
from core.analysis.artifacts import decode_artifact
from core.analysis.errors import AnalysisError
from core.analysis.forms import analysis_form
from core.analysis.services import process_run, resolve_inputs, start_run
from core.analysis.sources import output
from core.analysis.testing import local_ocr_response
from core.models import (
    AnalysisArtifact,
    AnalysisResult,
    AnalysisRun,
    ExtractedPassage,
    GatewayRequest,
)
from core.parsers import run_parser
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

    def start(self, role="owner", image=None):
        return start_run(
            self.users[role],
            self.case.pk,
            "plate_ocr",
            [{"kind": "image", "id": str((image or self.image).pk)}],
        )[0]

    def mixed_run(self, *, only_weak=False):
        original = plate_scene(distractors=True)
        image = ImageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic scored scene",
            file=ContentFile(original, name="scored-plate.jpeg"),
        )
        parsed = run_parser("plate_ocr", original)
        for region in parsed["candidates"]:
            if region["plate_score"] < 50:
                for attempt in region["attempts"]:
                    attempt["text"] = "<b>NOISE987</b>"
                    attempt["confidence"] = 100
        if only_weak:
            parsed["candidates"] = parsed["candidates"][:1]
            parsed["candidates"][0]["plate_score"] = 0
        run = self.start(image=image)
        with patch("image.analysis.plates.run_parser", return_value=parsed):
            process_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "partial", run.error_code)
        return image, run

    def test_weak_regions_are_hidden_without_renumbering_findings_or_leaking_text(self):
        image, run = self.mixed_run()
        self.assertEqual(ExtractedPassage.objects.filter(result=run.result).count(), 3)
        self.login("viewer")
        url = reverse("core:analysis:detail", args=[self.case.pk, run.pk])
        response = self.client.get(url)
        self.assertEqual(
            [item["index"] for item in response.context["items"]], [12, 13, 14]
        )
        self.assertEqual(response.context["items"][0]["index"], 12)
        self.assertEqual(response.context["weak_region_count"], 4)
        self.assertContains(response, "Show weak regions")
        self.assertNotContains(response, "NOISE987")
        self.assertContains(response, "Heuristic score, not a probability")
        shown = self.client.get(url, {"weak": "1"})
        self.assertEqual(len(shown.context["items"]), 15)
        self.assertTrue(all("weak=1" in item["url"] for item in shown.context["items"]))
        self.assertContains(shown, "&lt;b&gt;NOISE987&lt;/b&gt;")
        self.assertNotContains(shown, "<b>NOISE987</b>")
        self.assertTrue(shown.context["show_weak_regions"])
        explicit = self.client.get(url, {"finding": "2"})
        self.assertEqual(explicit.context["items"][0]["index"], 1)
        self.assertTrue(explicit.context["show_weak_regions"])
        strong = self.client.get(url, {"finding": "14"})
        self.assertEqual(strong.context["items"][0]["index"], 13)
        self.assertFalse(strong.context["show_weak_regions"])
        self.login("editor")
        reviewed = self.client.post(
            reverse("core:analysis:review", args=[self.case.pk, run.pk]) + "?weak=1",
            {
                "item_index": "13",
                "decision": "accepted",
                "comment": "Synthetic review",
            },
        )
        self.assertEqual(reviewed.status_code, 302)
        self.assertEqual(run.result.reviews.get().item_index, 13)
        self.assertIn("weak=1", reviewed["Location"])
        self.assertIn("finding=14", reviewed["Location"])
        derived, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "entities",
            [
                {
                    "kind": "image",
                    "id": str(image.pk),
                    "result_id": str(run.result.pk),
                }
            ],
        )
        self.assertNotIn("NOISE987", resolve_inputs(derived, self.case)[0].text)
        self.assertNotContains(
            self.client.get(self.url("list"), {"q": "NOISE987"}),
            "Synthetic scored scene",
        )
        self.login("outsider")
        self.assertEqual(self.client.get(url, {"weak": "1"}).status_code, 404)

    def test_all_weak_regions_have_recoverable_empty_state_and_zero_score(self):
        _, run = self.mixed_run(only_weak=True)
        self.login("viewer")
        url = reverse("core:analysis:detail", args=[self.case.pk, run.pk])
        response = self.client.get(url)
        self.assertEqual(response.context["items"], [])
        self.assertContains(response, "No regions met the plate filter")
        self.assertFalse(ExtractedPassage.objects.filter(result=run.result).exists())
        shown = self.client.get(url, {"weak": "1"})
        self.assertContains(shown, "Plate-likeness: 0/100")
        self.assertContains(shown, "Weak region")

    def test_historical_unscored_results_remain_visible(self):
        run = self.start()
        AnalysisRun.objects.filter(pk=run.pk).update(
            status="partial", method_version="1"
        )
        AnalysisResult.objects.create(
            case=self.case,
            run=run,
            data=output(
                "Legacy plate result",
                items=[
                    {
                        "label": "Historical reading",
                        "text": "LEGACY123",
                        "values": {},
                        "source_ids": [run.inputs[0]["id"]],
                        "anchors": [],
                    }
                ],
                outcome="partial",
            ),
        )
        self.login("viewer")
        response = self.client.get(
            reverse("core:analysis:detail", args=[self.case.pk, run.pk])
        )
        self.assertContains(response, "LEGACY123")
        self.assertNotContains(response, "Show weak regions")

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
        self.assertTrue(
            any("test 482" in text.casefold() for text in passages), passages
        )
        crops = list(run.result.artifacts.exclude(metadata={}))
        self.assertEqual(len(crops), 3)
        for crop in crops:
            finding = run.result.data["items"][crop.metadata["finding_index"]]
            self.assertEqual(crop.sha256, finding["values"]["Recognition JPEG SHA-256"])
        crop = crops[0]
        from django.urls import reverse

        url = reverse("core:analysis:artifact", args=[self.case.pk, run.pk, crop.pk])
        detail_url = (
            reverse("core:analysis:detail", args=[self.case.pk, run.pk]) + "?finding=1"
        )
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
