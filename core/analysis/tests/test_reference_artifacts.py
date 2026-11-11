"""Retained geographic responses use the existing private artifact boundary."""

import base64
import hashlib
import io
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from PIL import Image

from cases.models import Case, CaseMembership
from core.analysis.operations import OPERATIONS
from core.analysis.services import process_run, start_run
from core.analysis.sources import output
from core.models import AnalysisArtifact, AnalysisResult
from core.testing import EvidenceViewTestMixin
from geo.models import LocationEvidence


class GeographicReferenceFixtures(EvidenceViewTestMixin):
    raw_response = b' { "version": 0.6, "elements": [] }\r\n'

    def start(self, *, role="owner"):
        location = LocationEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic retained observation",
            latitude="43.650000",
            longitude="-79.380000",
        )
        run, _ = start_run(
            self.users[role],
            self.case.pk,
            "geographic_context",
            [{"kind": "location", "id": str(location.pk)}],
            external_processing=True,
        )
        return location, run

    def reference(self, run, *, raw=None, key="context-1"):
        source_id = run.inputs[0]["id"]
        return {
            "kind": "geographic_reference",
            "source_id": source_id,
            "content_type": "application/json",
            "data_base64": base64.b64encode(
                self.raw_response if raw is None else raw
            ).decode(),
            "metadata": {
                "reference_key": key,
                "source_ids": [source_id],
                "query": "[out:json];node(43.65,-79.38,43.66,-79.37);out;",
                "endpoint": "https://map.example.test/api/interpreter",
                "source_name": "Synthetic geographic reference",
                "reference_version": "Synthetic snapshot 2026-10",
                "attribution": "Synthetic reference data",
                "retrieved_at": "2026-10-28T12:00:00+00:00",
                "requested_as_of": None,
                "dataset_timestamp": "2026-10-28T11:59:00+00:00",
            },
        }

    def result_payload(self, run, artifacts):
        source_id = run.inputs[0]["id"]
        data = output(
            "Inspect retained geographic context.",
            items=[
                {
                    "label": "Synthetic nearby road",
                    "text": "Reference context does not establish presence.",
                    "source_ids": [source_id],
                    "anchors": [{"source_id": source_id}],
                    "values": {"Reference snapshot": "context-1"},
                }
            ],
        )
        data["artifacts"] = artifacts
        return data


class GeographicReferenceArtifactTests(GeographicReferenceFixtures, TestCase):
    def publish(self, run, artifacts=None):
        payload = self.result_payload(
            run, artifacts if artifacts is not None else [self.reference(run)]
        )
        with patch.dict(
            OPERATIONS,
            geographic_context=replace(
                OPERATIONS["geographic_context"], function=Mock(return_value=payload)
            ),
        ):
            process_run(str(run.pk))
        run.refresh_from_db()
        self.assertEqual(run.status, "complete", run.error_code)
        return run.result

    def analysis_url(self, name, run, *, case=None, **extra):
        return reverse(
            f"core:analysis:{name}",
            kwargs={"case_id": (case or self.case).pk, "run_id": run.pk, **extra},
        )

    def test_exact_response_and_query_provenance_are_retained_immutably(self):
        location, run = self.start()
        expected = self.reference(run)
        result = self.publish(run, [expected])
        artifact = AnalysisArtifact.objects.get(result=result)
        self.assertEqual(artifact.kind, "geographic_reference")
        self.assertIsNone(artifact.width)
        self.assertIsNone(artifact.height)
        self.assertEqual(str(artifact.source_id), run.inputs[0]["id"])
        self.assertEqual(artifact.metadata, expected["metadata"])
        self.assertEqual(artifact.sha256, hashlib.sha256(self.raw_response).hexdigest())
        with artifact.file.open("rb") as retained:
            self.assertEqual(retained.read(), self.raw_response)
        self.assertNotIn("artifacts", result.data)
        self.assertNotIn("data_base64", str(result.data))
        location.refresh_from_db()
        self.assertEqual(location.sha256, run.inputs[0]["sha256"])
        artifact.metadata = {"replacement": True}
        with self.assertRaises(ValidationError):
            artifact.save()

    def test_reference_download_rechecks_case_run_role_and_current_access(self):
        _, run = self.start()
        result = self.publish(run)
        artifact = AnalysisArtifact.objects.get(result=result)
        url = self.analysis_url("artifact", run, artifact_id=artifact.pk)
        self.assertEqual(self.client.get(url).status_code, 302)
        for role in ("owner", "editor", "viewer"):
            self.login(role)
            with self.subTest(role=role):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, self.raw_response)
                self.assertEqual(response["Content-Type"], "application/octet-stream")
                self.assertEqual(
                    response["Content-Disposition"],
                    f'attachment; filename="reference-{artifact.pk}.json"',
                )
                self.assertIn("private", response["Cache-Control"])
                self.assertIn("no-store", response["Cache-Control"])
                self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        _, other_run = self.start()
        self.login("viewer")
        self.assertEqual(
            self.client.get(
                self.analysis_url("artifact", other_run, artifact_id=artifact.pk)
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

    def test_stale_source_forged_anchor_and_changed_reference_bytes_are_unavailable(
        self,
    ):
        location, run = self.start()
        artifact = AnalysisArtifact.objects.get(result=self.publish(run))
        url = self.analysis_url("artifact", run, artifact_id=artifact.pk)
        self.login("viewer")
        LocationEvidence.objects.filter(pk=location.pk).update(sha256="f" * 64)
        self.assertEqual(self.client.get(url).status_code, 404)
        LocationEvidence.objects.filter(pk=location.pk).update(sha256=location.sha256)
        source_id = artifact.source_id
        AnalysisArtifact.objects.filter(pk=artifact.pk).update(source_id=uuid4())
        self.assertEqual(self.client.get(url).status_code, 404)
        AnalysisArtifact.objects.filter(pk=artifact.pk).update(source_id=source_id)
        self.assertEqual(self.client.get(url).status_code, 200)
        with artifact.file.open("wb") as retained:
            retained.write(b'{"changed": true}')
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_json_is_downloaded_while_legacy_preview_payload_stays_an_image(self):
        _, run = self.start()
        image = io.BytesIO()
        Image.new("RGB", (80, 60), "white").save(image, format="JPEG")
        # The existing OCR payload has no kind; it must remain a JPEG preview.
        preview = {
            "source_id": run.inputs[0]["id"],
            "content_type": "image/jpeg",
            "data_base64": base64.b64encode(image.getvalue()).decode(),
            "width": 80,
            "height": 60,
        }
        payload = self.result_payload(run, [preview, self.reference(run)])
        payload["items"][0]["anchors"][0]["region"] = [0.1, 0.1, 0.8, 0.8]
        with patch.dict(
            OPERATIONS,
            geographic_context=replace(
                OPERATIONS["geographic_context"], function=Mock(return_value=payload)
            ),
        ):
            process_run(str(run.pk))
        run.refresh_from_db()
        self.assertEqual(run.status, "complete", run.error_code)
        jpeg = AnalysisArtifact.objects.get(result=run.result, kind="image_preview")
        reference = AnalysisArtifact.objects.get(
            result=run.result, kind="geographic_reference"
        )
        self.login("viewer")
        jpeg_url = self.analysis_url("artifact", run, artifact_id=jpeg.pk)
        reference_url = self.analysis_url("artifact", run, artifact_id=reference.pk)
        response = self.client.get(self.analysis_url("detail", run))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["items"][0]["preview"]["url"], jpeg_url)
        self.assertContains(response, reference_url)
        self.assertNotContains(response, f'src="{reference_url}"')
        self.assertNotContains(response, f'href="{reference_url}" width=')
        downloaded = self.client.get(jpeg_url)
        self.assertEqual(downloaded["Content-Type"], "image/jpeg")
        self.assertTrue(downloaded["Content-Disposition"].startswith("inline;"))

    def test_model_and_database_enforce_dimensions_for_each_artifact_kind(self):
        _, run = self.start()
        result = self.publish(run)
        base = {
            "case": self.case,
            "result": result,
            "source_id": run.inputs[0]["id"],
            "sha256": "a" * 64,
            "file": "synthetic-reference.json",
            "metadata": self.reference(run)["metadata"],
        }
        invalid = (
            ("geographic_reference", 80, 60),
            ("geographic_reference", None, 60),
            ("image_preview", None, None),
            ("image_preview", 0, 60),
            ("image_preview", 5000, 60),
        )
        for kind, width, height in invalid:
            values = base | {"kind": kind, "width": width, "height": height}
            with self.subTest(kind=kind, width=width, height=height):
                with self.assertRaises(ValidationError):
                    AnalysisArtifact.objects.create(**values)
                # Maintenance/bulk paths must preserve the same dimension boundary.
                with self.assertRaises(IntegrityError), transaction.atomic():
                    AnalysisArtifact.objects.bulk_create([AnalysisArtifact(**values)])
        with self.assertRaises(ValidationError):
            AnalysisArtifact.objects.create(
                **(base | {"case": self.other_case}),
                kind="geographic_reference",
                width=None,
                height=None,
            )

    def test_invalid_or_oversized_json_cannot_publish_a_completed_reference(self):
        two_mib = 2 * 1024 * 1024
        for raw in (b"not JSON", b"[]", b'{"pad":"' + b"x" * two_mib + b'"}'):
            with self.subTest(size=len(raw)):
                _, run = self.start()
                payload = self.result_payload(run, [self.reference(run, raw=raw)])
                with patch.dict(
                    OPERATIONS,
                    geographic_context=replace(
                        OPERATIONS["geographic_context"],
                        function=Mock(return_value=payload),
                    ),
                ):
                    process_run(str(run.pk))
                run.refresh_from_db()
                self.assertEqual(run.status, "failed")
                self.assertFalse(AnalysisResult.objects.filter(run=run).exists())
                self.assertFalse(
                    any(path.is_file() for path in self.media_root.rglob("*"))
                )

    def test_reference_aggregate_limit_rolls_back_earlier_files(self):
        _, run = self.start()
        size = 2 * 1024 * 1024
        raw = b'{"padding":"' + b"x" * (size - 14) + b'"}'
        self.assertEqual(len(raw), size)
        artifacts = [
            self.reference(run, raw=raw, key=f"context-{index}")
            for index in range(1, 6)
        ]
        with patch.dict(
            OPERATIONS,
            geographic_context=replace(
                OPERATIONS["geographic_context"],
                function=Mock(return_value=self.result_payload(run, artifacts)),
            ),
        ):
            process_run(str(run.pk))
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        self.assertFalse(AnalysisResult.objects.filter(run=run).exists())
        self.assertFalse(AnalysisArtifact.objects.filter(result__run=run).exists())
        self.assertFalse(any(path.is_file() for path in self.media_root.rglob("*")))

    def test_publication_failure_removes_every_new_reference_file(self):
        _, run = self.start()
        artifacts = [self.reference(run), self.reference(run, key="context-2")]
        original_save = AnalysisArtifact.save
        saved = []

        def fail_second(instance, *args, **kwargs):
            if saved:
                raise ValidationError("Synthetic publication failure")
            original_save(instance, *args, **kwargs)
            saved.append(instance.pk)

        with (
            patch.dict(
                OPERATIONS,
                geographic_context=replace(
                    OPERATIONS["geographic_context"],
                    function=Mock(return_value=self.result_payload(run, artifacts)),
                ),
            ),
            patch(
                "core.analysis.services.AnalysisArtifact.save",
                autospec=True,
                side_effect=fail_second,
            ),
        ):
            process_run(str(run.pk))
        self.assertEqual(len(saved), 1)
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        self.assertFalse(AnalysisResult.objects.filter(run=run).exists())
        self.assertFalse(AnalysisArtifact.objects.filter(result__run=run).exists())
        self.assertFalse(any(path.is_file() for path in self.media_root.rglob("*")))

    def test_access_removed_during_processing_withholds_reference_publication(self):
        _, run = self.start(role="editor")
        payload = self.result_payload(run, [self.reference(run)])

        def revoke(*args, **kwargs):
            CaseMembership.objects.filter(
                case=self.case, user=self.users["editor"]
            ).delete()
            return deepcopy(payload)

        with patch.dict(
            OPERATIONS,
            geographic_context=replace(
                OPERATIONS["geographic_context"], function=Mock(side_effect=revoke)
            ),
        ):
            self.assertEqual(process_run(str(run.pk)), "access_removed")
        self.assertFalse(AnalysisResult.objects.filter(run=run).exists())
        self.assertFalse(any(path.is_file() for path in self.media_root.rglob("*")))

    def test_whole_case_cleanup_preserves_rollback_and_removes_committed_references(
        self,
    ):
        _, run = self.start()
        artifact = AnalysisArtifact.objects.get(result=self.publish(run))
        name, storage, case_id = artifact.file.name, artifact.file.storage, self.case.pk
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with self.assertRaisesMessage(ValueError, "rollback"), transaction.atomic():
                self.case.delete()
                raise ValueError("rollback")
        self.assertEqual(callbacks, [])
        self.assertTrue(storage.exists(name))
        self.assertTrue(AnalysisArtifact.objects.filter(pk=artifact.pk).exists())
        self.case = Case.objects.get(pk=case_id)
        with self.captureOnCommitCallbacks(execute=True):
            self.case.delete()
        self.assertFalse(storage.exists(name))
        self.assertFalse(AnalysisArtifact.objects.filter(pk=artifact.pk).exists())


class ArtifactCommitTests(GeographicReferenceFixtures, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.setUpTestData()

    def test_publication_exit_failures_preserve_committed_artifacts(self):
        real_atomic = transaction.atomic
        for failure in ("acknowledgement", "callback"):
            with self.subTest(failure=failure):
                _, run = self.start()
                contexts = 0

                def fail():
                    raise OSError("Synthetic private commit failure")

                @contextmanager
                def failing_publication(*args, failure=failure, **kwargs):
                    with real_atomic(*args, **kwargs):
                        yield
                        if failure == "callback":
                            transaction.on_commit(fail)
                    fail()

                def atomic_context(*args, **kwargs):
                    nonlocal contexts
                    contexts += 1
                    # The first transaction claims the queued run; the second
                    # publishes its complete result and files.
                    if contexts == 2:
                        return failing_publication(*args, **kwargs)
                    return real_atomic(*args, **kwargs)

                with (
                    patch.dict(
                        OPERATIONS,
                        geographic_context=replace(
                            OPERATIONS["geographic_context"],
                            function=Mock(
                                return_value=self.result_payload(
                                    run, [self.reference(run)]
                                )
                            ),
                        ),
                    ),
                    patch(
                        "core.analysis.services.transaction.atomic",
                        side_effect=atomic_context,
                    ),
                ):
                    self.assertEqual(process_run(str(run.pk)), "processing_failed")
                run.refresh_from_db()
                self.assertEqual(run.status, "complete")
                self.assertEqual(run.error_code, "")
                artifact = AnalysisArtifact.objects.get(result=run.result)
                with artifact.file.open("rb") as retained:
                    self.assertEqual(retained.read(), self.raw_response)
                self.assertEqual(process_run(str(run.pk)), "complete")
