"""Published extraction/search/preview boundaries and retained review provenance."""

import hashlib
import io
import json
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import TestCase
from django.urls import reverse
from PIL import Image, PngImagePlugin

from analysis.errors import AnalysisError
from analysis.models import (
    AnalysisArtifact,
    AnalysisResult,
    AnalysisRun,
    ExtractedPassage,
)
from analysis.services import process_run, resolve_inputs, review_result, start_run
from analysis.sources import output
from cases.models import Case, CaseMembership
from evidence.models import DocumentEvidence, ImageEvidence, LocationEvidence
from evidence.tests.test_views import EvidenceViewTestMixin


def image_bytes():
    stream = io.BytesIO()
    image = Image.new("RGB", (80, 60), "white")
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("Comment", "Synthetic original metadata")
    image.save(stream, format="PNG", pnginfo=metadata)
    return stream.getvalue()


def ocr_response(*, text="Synthetic extraction phrase", status="complete"):
    return {
        "response": json.dumps(
            {
                "status": status,
                "language": "en",
                "rotation_degrees": 0,
                "regions": [
                    {
                        "text": text,
                        "bbox": [0.1, 0.2, 0.8, 0.6],
                        "order": 0,
                        "confidence": "clear",
                        "alternatives": [],
                    }
                ],
            }
        )
    }


class ResultIntegrationTests(EvidenceViewTestMixin, TestCase):
    def image(self, *, case=None, **values):
        return ImageEvidence.objects.create(
            case=case or self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic retained image",
            file=SimpleUploadedFile(
                "synthetic.png", image_bytes(), content_type="image/png"
            ),
            width=999,
            height=888,
            **values,
        )

    def processed_ocr(self, image=None, **reply):
        image = image or self.image()
        run, _ = start_run(
            self.users["owner"],
            image.case_id,
            "ocr",
            [{"kind": "image", "id": str(image.pk)}],
            external_processing=True,
        )
        with patch(
            "analysis.gateway.GatewayClient.request", return_value=ocr_response(**reply)
        ):
            process_run(str(run.pk))
        run.refresh_from_db()
        self.assertIn(run.status, {"complete", "partial"}, run.error_code)
        return image, run, run.result, AnalysisArtifact.objects.get(result__run=run)

    def analysis_url(self, name, run, *, case=None, **extra):
        return reverse(
            f"analysis:{name}",
            kwargs={"case_id": (case or self.case).pk, "run_id": run.pk, **extra},
        )

    def test_real_image_ocr_publishes_private_preview_anchors_and_search_projection(
        self,
    ):
        original, run, result, artifact = self.processed_ocr()
        self.assertEqual(
            result.data["items"][0]["anchors"],
            [
                {
                    "source_id": run.inputs[0]["id"],
                    "region": [0.1, 0.2, 0.8, 0.6],
                    "coordinate_space": "original_unoriented_pixels",
                }
            ],
        )
        self.assertEqual(str(artifact.source_id), run.inputs[0]["id"])
        self.assertEqual(artifact.case, self.case)
        self.assertEqual((artifact.width, artifact.height), (80, 60))
        self.assertEqual(artifact.pk.version, 4)
        self.assertNotIn("artifacts", result.data)
        self.assertNotIn("data_base64", json.dumps(result.data))
        with artifact.file.open("rb") as retained:
            preview = retained.read()
        self.assertEqual(hashlib.sha256(preview).hexdigest(), artifact.sha256)
        self.assertTrue(preview.startswith(b"\xff\xd8\xff"))
        with Image.open(io.BytesIO(preview)) as decoded:
            self.assertEqual(decoded.format, "JPEG")
            self.assertEqual(decoded.getexif(), {})
            self.assertNotIn("Comment", decoded.info)
        original.refresh_from_db()
        self.assertEqual((original.width, original.height), (999, 888))
        with original.file.open("rb") as retained:
            self.assertEqual(retained.read(), image_bytes())
        passage = ExtractedPassage.objects.get(result=result)
        self.assertEqual(passage.evidence_sha256, original.sha256)
        self.assertEqual(passage.text, "Synthetic extraction phrase")
        self.assertEqual(result.provenance["source_inputs"], run.inputs)
        self.assertEqual(result.provenance["method_version"], run.method_version)
        self.login("viewer")
        response = self.client.get(
            self.url("list"), {"q": "Synthetic extraction phrase"}
        )
        self.assertContains(response, self.analysis_url("detail", run))
        self.assertContains(response, "Extracted content")
        detail = self.client.get(self.analysis_url("detail", run))
        self.assertContains(detail, "Retained image preview")
        self.assertContains(
            detail, self.analysis_url("artifact", run, artifact_id=artifact.pk)
        )

    def test_model_extracted_html_stays_escaped_in_results_and_search_excerpts(self):
        text = "<img src=x onerror=alert(1)> Synthetic untrusted extraction"
        original, run, result, artifact = self.processed_ocr(text=text)
        self.login("viewer")
        for response in (
            self.client.get(self.analysis_url("detail", run)),
            self.client.get(self.url("list"), {"q": "Synthetic untrusted extraction"}),
        ):
            self.assertContains(response, "&lt;img src=x onerror=alert(1)&gt;")
            self.assertNotContains(response, "<img src=x onerror=alert(1)>")

    def test_private_preview_route_rechecks_current_case_run_and_original_access(self):
        original, run, result, artifact = self.processed_ocr()
        url = self.analysis_url("artifact", run, artifact_id=artifact.pk)
        for role, expected in (
            ("owner", 200),
            ("editor", 200),
            ("viewer", 200),
            ("outsider", 404),
        ):
            self.login(role)
            with self.subTest(role=role):
                response = self.client.get(url)
                self.assertEqual(response.status_code, expected)
                self.assertIn("private", response["Cache-Control"])
                self.assertIn("no-store", response["Cache-Control"])
                self.assertEqual(response["X-Content-Type-Options"], "nosniff")
                if expected == 200:
                    self.assertEqual(response["Content-Type"], "image/jpeg")
                    self.assertTrue(
                        response["Content-Disposition"].startswith("inline;")
                    )
        self.login("viewer")
        second, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "image_metadata",
            [{"kind": "image", "id": str(original.pk)}],
        )
        self.assertEqual(
            self.client.get(
                self.analysis_url("artifact", second, artifact_id=artifact.pk)
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                self.analysis_url("artifact", run, artifact_id=uuid4())
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                self.analysis_url(
                    "artifact", run, case=self.other_case, artifact_id=artifact.pk
                )
            ).status_code,
            404,
        )

        CaseMembership.objects.filter(
            case=self.case, user=self.users["viewer"]
        ).delete()
        self.assertEqual(self.client.get(url).status_code, 404)
        self.users["outsider"].is_superuser = True
        self.users["outsider"].save()
        self.login("outsider")
        self.assertEqual(self.client.get(url).status_code, 404)
        self.login("owner")
        ImageEvidence.objects.filter(pk=original.pk).update(sha256="f" * 64)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_preview_checksum_and_source_identity_cannot_be_forged_for_read(self):
        original, run, result, artifact = self.processed_ocr()
        self.login("viewer")
        url = self.analysis_url("artifact", run, artifact_id=artifact.pk)
        stored_source_id = artifact.source_id
        AnalysisArtifact.objects.filter(pk=artifact.pk).update(source_id=uuid4())
        self.assertEqual(self.client.get(url).status_code, 404)
        AnalysisArtifact.objects.filter(pk=artifact.pk).update(
            source_id=stored_source_id
        )
        self.assertEqual(self.client.get(url).status_code, 200)
        with artifact.file.open("wb") as retained:
            retained.write(b"Changed private preview")
        self.assertEqual(self.client.get(url).status_code, 404)
        artifact.width = 1
        with self.assertRaises(ValidationError):
            artifact.save()
        with self.assertRaises(ValidationError):
            AnalysisArtifact.objects.create(
                case=self.case,
                result=result,
                source_id=uuid4(),
                sha256="a" * 64,
                width=80,
                height=60,
            )
        with self.assertRaises(ValidationError):
            AnalysisArtifact.objects.create(
                case=self.other_case,
                result=result,
                source_id=run.inputs[0]["id"],
                sha256="a" * 64,
                width=80,
                height=60,
            )

    def test_failed_publication_removes_new_preview_files_and_rolls_back_result(self):
        image = self.image()
        original_path = image.file.name
        before = {
            p.relative_to(self.media_root)
            for p in self.media_root.rglob("*")
            if p.is_file()
        }
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "ocr",
            [{"kind": "image", "id": str(image.pk)}],
            external_processing=True,
        )
        with (
            patch(
                "analysis.gateway.GatewayClient.request", return_value=ocr_response()
            ),
            patch(
                "analysis.services.AnalysisArtifact.save",
                side_effect=ValidationError("Synthetic publication failure"),
            ),
        ):
            self.assertEqual(process_run(str(run.pk)), "processing_failed")
        self.assertFalse(AnalysisResult.objects.filter(run=run).exists())
        self.assertFalse(AnalysisArtifact.objects.filter(result__run=run).exists())
        self.assertTrue(image.file.storage.exists(original_path))
        after = {
            p.relative_to(self.media_root)
            for p in self.media_root.rglob("*")
            if p.is_file()
        }
        self.assertEqual(before, after)
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")

    def test_whole_case_cleanup_runs_after_commit_and_rollback_preserves_preview(self):
        original, run, result, artifact = self.processed_ocr()
        name, storage = artifact.file.name, artifact.file.storage
        original_name = original.file.name
        case_id = self.case.pk
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with self.assertRaisesMessage(ValueError, "rollback"), transaction.atomic():
                self.case.delete()
                raise ValueError("rollback")
        self.assertEqual(callbacks, [])
        self.assertTrue(storage.exists(name))
        self.assertTrue(AnalysisArtifact.objects.filter(pk=artifact.pk).exists())
        # A rolled-back delete still clears the Python instance primary key.
        self.case = Case.objects.get(pk=case_id)
        with self.captureOnCommitCallbacks(execute=True):
            self.case.delete()
        self.assertFalse(storage.exists(name))
        self.assertFalse(storage.exists(original_name))
        self.assertFalse(AnalysisArtifact.objects.filter(pk=artifact.pk).exists())

    def test_native_extraction_partial_search_preserves_exact_result_version_link(self):
        document = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Supported native source",
            file=SimpleUploadedFile("source.txt", b"Unique native search phrase\n"),
        )
        unsupported = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Unsupported binary",
            file=SimpleUploadedFile("source.docx", b"PK\x03\x04Unsupported container"),
        )
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "native_text",
            [
                {"kind": "document", "id": str(value.pk)}
                for value in (document, unsupported)
            ],
        )
        process_run(str(run.pk))
        run.refresh_from_db()
        self.assertEqual(run.status, "partial", run.error_code)
        self.assertEqual(ExtractedPassage.objects.filter(result=run.result).count(), 1)
        self.login("viewer")
        response = self.client.get(
            self.url("list"), {"q": "Unique native search phrase"}
        )
        self.assertContains(response, "Extracted content · Partial coverage")
        self.assertContains(response, self.analysis_url("detail", run))
        self.assertNotContains(response, "Unsupported binary")
        self.assertEqual(len(response.context["rows"]), 1)
        DocumentEvidence.objects.filter(pk=document.pk).update(sha256="f" * 64)
        response = self.client.get(
            self.url("list"), {"q": "Unique native search phrase"}
        )
        self.assertEqual(len(response.context["rows"]), 0)

    def geocode_fixture(self):
        location = LocationEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Address-only observation",
            address="1 Synthetic Road",
        )
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "geocode_locations",
            [{"kind": "location", "id": str(location.pk)}],
            external_processing=True,
        )
        AnalysisRun.objects.filter(pk=run.pk).update(status="complete", progress=100)
        run.refresh_from_db()
        source = run.inputs[0]["id"]
        result = AnalysisResult.objects.create(
            case=self.case,
            run=run,
            data=output(
                "Geocode candidates",
                items=[
                    {
                        "label": "Candidate estimate",
                        "text": "Derived interpretation",
                        "source_ids": [source],
                        "anchors": [{"source_id": source}],
                        "values": {"latitude": 43, "longitude": -79},
                    }
                ],
            ),
        )
        review = review_result(
            self.users["editor"],
            self.case.pk,
            result.pk,
            "accepted",
            "Synthetic reference checked",
            item_index=0,
        )
        return location, run, result, review

    def test_downstream_geocode_retains_review_id_and_historical_interpretation(
        self,
    ):
        location, prior, result, review = self.geocode_fixture()
        selections = [
            {
                "kind": "location",
                "id": str(location.pk),
                "result_id": str(result.pk),
                "geocode_candidate": 0,
            }
        ]
        finished, _ = start_run(
            self.users["owner"], self.case.pk, "spatial_groups", selections
        )
        queued, _ = start_run(
            self.users["owner"], self.case.pk, "spatial_groups", selections
        )
        original_inputs = deepcopy(finished.inputs)
        self.assertEqual(original_inputs[0]["review_id"], str(review.pk))
        process_run(str(finished.pk))
        finished.refresh_from_db()
        self.assertEqual(finished.status, "complete")
        self.assertEqual(finished.result.provenance["source_inputs"], original_inputs)
        review_result(
            self.users["owner"],
            self.case.pk,
            result.pk,
            "rejected",
            "Later competing interpretation",
            item_index=0,
        )
        with self.assertRaisesMessage(AnalysisError, "geocode_review_required"):
            resolve_inputs(finished, self.case)
        historical = resolve_inputs(finished, self.case, require_current_review=False)
        self.assertEqual(historical[0].derived_coordinates, (43.0, -79.0))
        self.assertEqual(process_run(str(queued.pk)), "geocode_review_required")
        with self.assertRaisesMessage(AnalysisError, "geocode_review_required"):
            start_run(self.users["owner"], self.case.pk, "spatial_groups", selections)
        finished.refresh_from_db()
        self.assertEqual(finished.inputs, original_inputs)
        self.login("viewer")
        response = self.client.get(self.analysis_url("detail", finished))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "different current review")
        self.assertContains(response, "Spatial group 1")
        downloaded = self.client.get(self.analysis_url("download", finished))
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.json()["inputs"][0]["review_id"], str(review.pk))
        self.assertEqual(downloaded.json()["result"], finished.result.data)
