"""Deliberate inference location and third-party approval in investigator forms."""

from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse

from audio.models import AudioEvidence
from core.analysis.forms import analysis_form
from core.models import AnalysisRun
from core.testing import EvidenceViewTestMixin
from geo.models import LocationEvidence
from phone.models import TextMessageEvidence


class LocalAnalysisFormTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.note = self.text()
        self.message = TextMessageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic message",
            sender=self.person,
            recipient=self.person,
            body="The proposed meeting is unconfirmed.",
        )
        self.audio = AudioEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic recording",
            file=ContentFile(b"synthetic", name="recording.wav"),
        )
        self.location = LocationEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic location",
            address="Synthetic Road",
        )

    def form(self, operation, kind="text", item=None, **values):
        return analysis_form(
            {
                "operation": operation,
                "submission_id": uuid4(),
                "sources": [f"{kind}:{(item or self.note).pk}"],
            }
            | values,
            case=self.case,
            operation=operation,
        )

    def test_all_five_model_paths_default_local_and_cloud_requires_approval(self):
        methods = (
            ("transcription", "audio", self.audio, {}),
            ("content_events", "text", self.note, {}),
            ("session_summaries", "text-message", self.message, {}),
            ("text_topics", "text", self.note, {"include_summary": True}),
            (
                "conversation_link",
                "text-message",
                self.message,
                {"semantic_links": True},
            ),
        )
        for operation, kind, item, values in methods:
            with self.subTest(operation=operation):
                local = self.form(operation, kind, item, **values)
                self.assertTrue(local.is_valid(), local.errors)
                self.assertEqual(local.parameters()["backend"], "local")
                self.assertFalse(local.cleaned_data["external_processing"])
                self.assertEqual(local.fields["backend"].initial, "local")
                unapproved = self.form(
                    operation, kind, item, backend="gateway", **values
                )
                self.assertFalse(unapproved.is_valid())
                self.assertIn("external_processing", unapproved.errors)
                approved = self.form(
                    operation,
                    kind,
                    item,
                    backend="gateway",
                    external_processing=True,
                    **values,
                )
                self.assertTrue(approved.is_valid(), approved.errors)
                self.assertTrue(approved.cleaned_data["external_processing"])
                self.assertEqual(approved.parameters()["backend"], "gateway")

    def test_local_approval_cannot_enable_cloud_or_deterministic_ai_options(self):
        local = self.form("content_events", external_processing=True)
        self.assertTrue(local.is_valid(), local.errors)
        self.assertFalse(local.cleaned_data["external_processing"])
        self.assertEqual(local.parameters()["backend"], "local")
        for operation, values in (
            ("entities", {"include_summary": True}),
            ("text_topics", {"include_summary": False}),
            ("conversation_link", {"semantic_links": False}),
        ):
            kind = "text-message" if operation == "conversation_link" else "text"
            item = self.message if kind == "text-message" else self.note
            form = self.form(
                operation,
                kind,
                item,
                backend="gateway",
                external_processing=True,
                **values,
            )
            self.assertTrue(form.is_valid(), form.errors)
            self.assertFalse(form.cleaned_data["external_processing"])
            self.assertNotIn("backend", form.parameters())
        invalid = self.form("content_events", backend="unapproved-runtime")
        self.assertFalse(invalid.is_valid())
        self.assertIn("backend", invalid.errors)

    def test_geo_approval_depends_on_explicit_operator_trust(self):
        for operation, setting in (
            ("geocode_locations", "PANDECT_GEOCODER_LOCAL"),
            ("geographic_context", "PANDECT_MAP_CONTEXT_LOCAL"),
        ):
            with self.subTest(operation=operation):
                with override_settings(**{setting: False}):
                    untrusted = self.form(operation, "location", self.location)
                    self.assertFalse(untrusted.is_valid())
                    self.assertIn("external_processing", untrusted.errors)
                with override_settings(**{setting: True}):
                    private = self.form(operation, "location", self.location)
                    self.assertTrue(private.is_valid(), private.errors)
                    self.assertFalse(private.cleaned_data["external_processing"])
                    self.assertTrue(
                        private.fields["external_processing"].widget.is_hidden
                    )

    def test_evidence_additional_analysis_uses_local_by_default(self):
        self.login("owner")
        url = reverse(
            "core:analysis:external_evidence",
            args=(self.case.pk, "text", self.note.pk),
        )
        response = self.client.get(url, {"operation": "content_events"})
        self.assertContains(response, "Suggest claims and commitments")
        self.assertContains(response, "Processing location")
        self.assertEqual(response.context["form"]["backend"].value(), "local")
        with patch(
            "core.analysis.views.start_run",
            return_value=(SimpleNamespace(pk=uuid4()), True),
        ) as start:
            response = self.client.post(
                url,
                {
                    "operation": "content_events",
                    "submission_id": uuid4(),
                    "sources": [f"text:{self.note.pk}"],
                },
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(start.call_args.kwargs["parameters"], {"backend": "local"})
        self.assertFalse(start.call_args.kwargs["external_processing"])
        self.assertFalse(AnalysisRun.objects.exists())

    def test_retained_history_shows_route_without_exposing_private_endpoint(self):
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.users["owner"],
            operation="content_events",
            method_version="1",
            parameters={
                "backend": "local",
                "_execution": {
                    "backend": "local",
                    "location": "local",
                    "model": "Synthetic local model",
                    "runtime": "synthetic-runtime-revision",
                    "url": "http://private-inference.example.test:9000",
                },
            },
            inputs=[
                {
                    "id": f"text:{self.note.pk}",
                    "kind": "text",
                    "evidence_id": str(self.note.pk),
                    "sha256": self.note.sha256,
                }
            ],
        )
        self.login("viewer")
        response = self.client.get(
            reverse("core:analysis:detail", args=(self.case.pk, run.pk))
        )
        self.assertContains(response, "Local processing")
        self.assertContains(response, "Synthetic local model")
        self.assertContains(response, "synthetic-runtime-revision")
        self.assertNotContains(response, "private-inference.example.test")
        self.assertNotContains(response, "predates execution pinning")
        self.assertNotContains(response, "Start another run")
