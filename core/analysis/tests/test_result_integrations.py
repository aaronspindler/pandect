"""Published extraction/search/preview boundaries and retained review provenance."""

import hashlib
import io
import json
from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection, transaction
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from cases.models import Case, CaseMembership
from core.analysis.address_estimates import set_address_estimate
from core.analysis.catalogue import project_result
from core.analysis.errors import AnalysisError
from core.analysis.estimate_dependencies import verified_estimate_uses
from core.analysis.operations import get_operation
from core.analysis.services import start_run
from core.analysis.source_resolution import resolve_inputs
from core.analysis.sources import output
from core.analysis.testing import external_grant_kwargs, local_ocr_response
from core.analysis.testing import preview_image_bytes as image_bytes
from core.analysis.worker import process_run
from core.models import (
    AnalysisArtifact,
    AnalysisResult,
    AnalysisReview,
    AnalysisRun,
    ExtractedPassage,
    GatewayRequest,
)
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from geo.models import LocationEvidence
from geo.testing import LOCAL_GEOCODER
from image.models import ImageEvidence
from image.testing import printed_layout
from people.models import Person
from phone.models import CommunicationContext, TextMessageEvidence
from socialmedia.models import SocialAccount


class ResultIntegrationTests(EvidenceViewTestMixin, TestCase):
    def test_malformed_worker_output_records_a_safe_specific_failure(self):
        original = self.image()
        operation = get_operation("image_metadata")
        for result, code in (
            ([], "invalid_result"),
            (output("Synthetic", outcome=[]), "invalid_result"),
            (output("Synthetic") | {"metrics": []}, "invalid_result"),
            (
                output("Synthetic", items=[{"label": "Finding", "values": []}]),
                "invalid_result",
            ),
            (
                output("Synthetic", metrics={"bad number": float("nan")}),
                "invalid_result",
            ),
            (
                output("Synthetic", items=[{"label": "Finding", "source_ids": [{}]}]),
                "invalid_anchor",
            ),
        ):
            with self.subTest(code=code, result=result):
                run, _ = start_run(
                    self.users["owner"],
                    self.case.pk,
                    "image_metadata",
                    [{"kind": "image", "id": str(original.pk)}],
                )
                with patch(
                    "core.analysis.worker.get_operation",
                    return_value=replace(operation, function=Mock(return_value=result)),
                ):
                    self.assertEqual(process_run(run.pk), code)
                run.refresh_from_db()
                self.assertEqual((run.status, run.error_code), ("failed", code))
                self.assertFalse(AnalysisResult.objects.filter(run=run).exists())

    def test_multi_source_extraction_publishes_search_passages_in_one_insert(self):
        documents = [
            DocumentEvidence.objects.create(
                case=self.case,
                uploaded_by=self.users["owner"],
                title=f"Source {index}",
                file=SimpleUploadedFile(f"{index}.txt", f"Source {index}".encode()),
            )
            for index in range(3)
        ]
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "native_text",
            [{"kind": "document", "id": str(item.pk)} for item in documents],
        )
        with CaptureQueriesContext(connection) as queries:
            process_run(run.pk)
        inserts = [
            query["sql"]
            for query in queries.captured_queries
            if query["sql"].startswith('INSERT INTO "core_extractedpassage"')
        ]
        self.assertEqual(len(inserts), 1)
        self.assertEqual(
            list(
                run.result.passages.order_by("item_index").values_list(
                    "evidence_id", "evidence_sha256", "text"
                )
            ),
            [(item.pk, item.sha256, item.title) for item in documents],
        )

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
        )
        with patch(
            "image.analysis.ai.run_parser", return_value=local_ocr_response(**reply)
        ):
            process_run(str(run.pk))
        run.refresh_from_db()
        self.assertIn(run.status, {"complete", "partial"}, run.error_code)
        self.assertFalse(run.external_processing)
        self.assertFalse(GatewayRequest.objects.filter(run=run).exists())
        return image, run, run.result, AnalysisArtifact.objects.get(result__run=run)

    def test_old_queued_ocr_cannot_dispatch_remotely_or_relabel_its_method(self):
        image = self.image()
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "ocr",
            [{"kind": "image", "id": str(image.pk)}],
        )
        self.assertFalse(run.external_processing)
        AnalysisRun.objects.filter(pk=run.pk).update(
            method_version="2", external_processing=True
        )
        with patch("core.analysis.gateway.GatewayClient.request") as gateway:
            self.assertEqual(process_run(run.pk), "processing_version_unavailable")
            gateway.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.method_version, "2")
        self.assertFalse(AnalysisResult.objects.filter(run=run).exists())

    def test_multi_source_layout_preserves_anchors_and_literal_message_labels(self):
        selections = []
        for model, kind, fixture in (
            (ImageEvidence, "image", "chat"),
            (DocumentEvidence, "document", "form"),
        ):
            content, _ = printed_layout(fixture)
            item = model.objects.create(
                case=self.case,
                uploaded_by=self.users["owner"],
                title=f"Synthetic {fixture}",
                file=SimpleUploadedFile(f"{fixture}.png", content),
            )
            selections.append({"kind": kind, "id": str(item.pk)})
        run, _ = start_run(self.users["editor"], self.case.pk, "ocr", selections)
        with patch("core.analysis.gateway.GatewayClient.request") as gateway:
            process_run(run.pk)
            gateway.assert_not_called()
        run.refresh_from_db()
        self.assertIn(run.status, {"complete", "partial"}, run.error_code)
        result = run.result
        self.assertEqual(process_run(run.pk), run.status)
        self.assertEqual(run.method_version, "6")
        self.assertEqual(result.provenance["source_inputs"], run.inputs)
        self.assertEqual(result.provenance["method_version"], "6")
        for source, kind in zip(run.inputs, ("message", "form_field"), strict=True):
            layout = result.data["visible_structure"][source["id"]]
            self.assertIn(
                kind, {candidate["kind"] for candidate in layout["candidates"]}
            )
            for word in layout["words"]:
                line = result.data["items"][word["item"]]
                self.assertEqual(line["source_ids"], [source["id"]])
                self.assertEqual(line["values"]["Input SHA-256"], source["sha256"])
                self.assertTrue(line["text"][word["start"] : word["end"]])
        self.assertQuerySetEqual(
            result.passages.order_by("item_index").values_list("text", flat=True),
            [line["text"] for line in result.data["items"]],
        )
        self.assertFalse(TextMessageEvidence.objects.filter(case=self.case).exists())
        self.assertFalse(CommunicationContext.objects.filter(case=self.case).exists())
        self.assertFalse(SocialAccount.objects.filter(person__case=self.case).exists())
        self.assertEqual(list(Person.objects.filter(case=self.case)), [self.person])
        self.login("viewer")
        url = self.analysis_url("download", run)
        self.assertEqual(
            json.loads(self.client.get(url).content)["result"]["visible_structure"],
            result.data["visible_structure"],
        )
        CaseMembership.objects.filter(
            case=self.case, user=self.users["viewer"]
        ).delete()
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_layout_at_publication_limit_preserves_recognized_lines(self):
        original, _, baseline, _ = self.processed_ocr()
        bare = dict(baseline.data)
        del bare["visible_structure"]
        limit = len(json.dumps(bare).encode()) + 100
        with patch("core.analysis.result_validation.MAX_RESULT_BYTES", limit):
            _, run, result, _ = self.processed_ocr(image=original)
        self.assertEqual(run.status, "partial")
        self.assertNotIn("visible_structure", result.data)
        self.assertEqual(result.data["metrics"]["Partial/unreadable inputs"], 1)
        self.assertIn(
            "Visible structure omitted at the result size limit.",
            result.data["warnings"],
        )
        self.assertEqual(result.data["items"], bare["items"])
        self.assertEqual(result.passages.get().text, "Synthetic extraction phrase")

    def test_missing_word_geometry_fails_privately_before_publication(self):
        original = self.image()
        run, _ = start_run(
            self.users["editor"],
            self.case.pk,
            "ocr",
            [{"kind": "image", "id": str(original.pk)}],
        )
        literal = "Sensitive synthetic wording"
        prepared = local_ocr_response(text=literal)
        del prepared["regions"][0]["words"]
        with (
            patch("image.analysis.ai.run_parser", return_value=prepared),
            self.assertLogs("core.analysis.failures", level="ERROR") as logs,
        ):
            self.assertEqual(process_run(run.pk), "ocr_invalid_output")
        self.assertNotIn(literal, "\n".join(logs.output))
        self.assertFalse(AnalysisResult.objects.filter(run=run).exists())
        self.assertFalse(AnalysisArtifact.objects.filter(result__run=run).exists())

    def test_saved_v5_lines_stay_inspectable_after_new_layout_run(self):
        original = self.image()
        choices = [{"kind": "image", "id": str(original.pk)}]
        previous, _ = start_run(self.users["owner"], self.case.pk, "ocr", choices)
        AnalysisRun.objects.filter(pk=previous.pk).update(
            method_version="5", status="complete"
        )
        previous.refresh_from_db()
        (source,) = resolve_inputs(previous, self.case)
        data = output(
            "Earlier OCR",
            items=[
                {
                    "source_ids": [source.id],
                    "label": "Text candidate",
                    "text": "Earlier reading",
                    "anchors": [
                        source.anchor(
                            region=[0.1, 0.2, 0.8, 0.6],
                            coordinate_space="original_unoriented_pixels",
                        )
                    ],
                }
            ],
        )
        historical = AnalysisResult.objects.create(
            case=self.case, run=previous, data=data
        )
        current, _ = start_run(
            self.users["editor"], self.case.pk, "ocr", choices, previous_run=previous
        )
        with patch("image.analysis.ai.run_parser", return_value=local_ocr_response()):
            process_run(current.pk)
        self.assertIn("visible_structure", current.result.data)
        historical.refresh_from_db()
        self.assertEqual(historical.data, data)
        self.login("viewer")
        detail = self.client.get(self.analysis_url("detail", previous))
        self.assertContains(detail, "Earlier reading")
        self.assertNotContains(detail, "Visible structure")

    def analysis_url(self, name, run, *, case=None, **extra):
        return reverse(
            f"core:analysis:{name}",
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

    def test_no_text_ocr_publishes_coverage_without_searchable_candidate_text(self):
        original = self.image()
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "ocr",
            [{"kind": "image", "id": str(original.pk)}],
        )
        reply = local_ocr_response(status="partial") | {"regions": []}
        with patch("image.analysis.ai.run_parser", return_value=reply):
            process_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "partial", run.error_code)
        self.assertFalse(ExtractedPassage.objects.filter(result=run.result).exists())
        self.assertEqual(run.result.data["metrics"]["Recognized text regions"], 0)
        self.login("viewer")
        response = self.client.get(self.analysis_url("detail", run))
        self.assertContains(
            response, "No text recognized. Inspect the original images."
        )
        self.assertNotContains(response, "Produced 1 source-linked OCR findings")

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
        self.client.post(self.url("archive", original, kind="image"))
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.post(self.url("restore", original, kind="image"))
        self.assertEqual(self.client.get(url).status_code, 200)
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
        )
        with (
            patch("image.analysis.ai.run_parser", return_value=local_ocr_response()),
            patch(
                "core.analysis.worker.AnalysisArtifact.save",
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

    @override_settings(**(LOCAL_GEOCODER | {"PANDECT_GEOCODER_LOCAL": False}))
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
            **external_grant_kwargs(self.case, "geocode_locations"),
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
        set_address_estimate(self.users["editor"], self.case.pk, run.pk, 0, use=True)
        review = result.reviews.get()
        return location, run, result, review

    @override_settings(
        PANDECT_MAP_TILE_URL="https://tiles.example.test/{z}/{x}/{y}.png",
        PANDECT_MAP_TILE_ORIGIN="https://tiles.example.test",
    )
    def test_observation_map_requires_an_accessible_location_result(self):
        location, run, _, _ = self.geocode_fixture()
        tile_origin = "https://tiles.example.test"
        url = self.analysis_url("detail", run) + "?page=1&finding=1"
        self.login("viewer")
        response = self.client.get(url)
        self.assertContains(response, "Selected observation positions")
        self.assertIn(tile_origin, response["Content-Security-Policy"])
        self.assertTrue(response.context["position_map_available"])
        self.assertFalse(response.context["position_plot"])
        self.assertEqual(response.context["map_points"][0]["kind"], "reviewed")

        CaseMembership.objects.get(case=self.case, user=self.users["viewer"]).delete()
        denied = self.client.get(url)
        self.assertEqual(denied.status_code, 404)
        self.assertNotIn(tile_origin, denied["Content-Security-Policy"])

        self.login("owner")
        LocationEvidence.objects.filter(pk=location.pk).update(
            is_archived=True,
            archive_changed_at=timezone.now(),
            archive_changed_by=self.users["owner"],
        )
        withheld = self.client.get(url)
        self.assertNotContains(withheld, "Selected observation positions")
        self.assertNotIn(tile_origin, withheld["Content-Security-Policy"])

    def test_estimate_priority_requires_a_readable_published_use(self):
        location, _, result, _ = self.geocode_fixture()
        project_result(result)
        candidates = result.findings.filter(case=self.case)
        candidate = candidates.get()
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "spatial_groups",
            [{"kind": "location", "id": str(location.pk), "result_id": str(result.pk)}],
        )
        self.assertEqual(verified_estimate_uses(candidates, self.case), {})
        process_run(str(run.pk))
        self.assertEqual(
            verified_estimate_uses(candidates, self.case), {candidate.pk: run.pk}
        )
        LocationEvidence.objects.filter(pk=location.pk).update(
            is_archived=True,
            archive_changed_at=timezone.now(),
            archive_changed_by=self.users["owner"],
        )
        self.assertEqual(verified_estimate_uses(candidates, self.case), {})

    def test_geocode_source_resolution_ignores_reviews_from_other_cases(self):
        location, _, result, review = self.geocode_fixture()
        selections = [
            {
                "kind": "location",
                "id": str(location.pk),
                "result_id": str(result.pk),
            }
        ]
        AnalysisReview.objects.bulk_create(
            [
                AnalysisReview(
                    case=self.other_case,
                    result=result,
                    reviewed_by=self.users["outsider"],
                    decision="rejected",
                    item_index=0,
                )
            ]
        )
        queued, _ = start_run(
            self.users["owner"], self.case.pk, "spatial_groups", selections
        )
        self.assertEqual(queued.inputs[0]["review_id"], str(review.pk))
        self.assertEqual(
            resolve_inputs(queued, self.case)[0].derived_coordinates, (43.0, -79.0)
        )
        AnalysisReview.objects.filter(pk=review.pk).update(case=self.other_case)
        for require_current_review in (True, False):
            with (
                self.subTest(require_current_review=require_current_review),
                self.assertRaisesMessage(AnalysisError, "geocode_review_required"),
            ):
                resolve_inputs(
                    queued, self.case, require_current_review=require_current_review
                )

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
        set_address_estimate(self.users["owner"], self.case.pk, prior.pk, 0, use=False)
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
        self.assertContains(response, "is no longer selected")
        self.assertContains(response, "Spatial group 1")
        downloaded = self.client.get(self.analysis_url("download", finished))
        self.assertEqual(downloaded.status_code, 200)
        data = json.loads(downloaded.content)
        self.assertEqual(data["inputs"][0]["review_id"], str(review.pk))
        self.assertEqual(data["result"], finished.result.data)
