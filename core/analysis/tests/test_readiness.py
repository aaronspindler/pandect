"""Honest, bounded source prerequisites without decoding or network probes."""

from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from django.core.files.base import ContentFile
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from core.analysis.forms import analysis_form
from core.analysis.operations import get_operation
from core.analysis.readiness import configuration_readiness, source_readiness
from core.analysis.services import process_run, start_run
from core.models import AnalysisResult, AnalysisRun
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from geo.models import LocationEvidence
from phone.models import CallEvidenceAttachment, PhoneCallEvidence


class AnalysisReadinessTests(EvidenceViewTestMixin, TestCase):
    def document(self, **kwargs):
        return DocumentEvidence.objects.create(
            case=kwargs.pop("case", self.case),
            uploaded_by=self.users["owner"],
            title="Synthetic document",
            file=ContentFile(b"Exact synthetic text", name="source.txt"),
            **kwargs,
        )

    def retained(
        self,
        item,
        *,
        operation="native_text",
        status="complete",
        kind="document",
        attachment=None,
        reason=None,
        version=None,
        extra_input=None,
    ):
        selected = {
            "id": str(uuid4()),
            "kind": kind,
            "evidence_id": str(item.pk),
            "sha256": attachment.sha256 if attachment else item.sha256,
        }
        if attachment:
            selected["attachment_id"] = str(attachment.pk)
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.users["owner"],
            operation=operation,
            method_version=version or get_operation(operation).version,
            status=status,
            inputs=[selected] + ([extra_input] if extra_input else []),
        )
        AnalysisResult.objects.create(
            case=self.case,
            run=run,
            data={"items": [{"values": {"Status": reason}}] if reason else []},
        )
        return run

    def test_file_inspections_are_exact_current_single_source_and_batched(self):
        items = [self.document() for _ in range(30)]
        for item in items:
            self.retained(item)
        choices = [(f"document:{item.pk}", item.title) for item in items]
        with CaptureQueriesContext(connection) as queries:
            ready = source_readiness(self.case, "native_text", choices)
        self.assertTrue(all(value["label"] == "Ready" for value in ready.values()))
        self.assertLessEqual(len(queries), 2)
        item = items[0]
        choice = choices[:1]
        self.retained(
            item,
            status="unsupported",
            version="old-method",
            reason="unsupported_document_format",
        )
        self.retained(
            item,
            status="unsupported",
            reason="unsupported_document_format",
            extra_input={
                "kind": "document",
                "evidence_id": str(items[1].pk),
                "sha256": items[1].sha256,
            },
        )
        self.assertEqual(
            source_readiness(self.case, "native_text", choice)[choice[0][0]]["label"],
            "Ready",
        )
        self.retained(item, status="unsupported", reason="unsupported_document_format")
        state = source_readiness(self.case, "native_text", choice)[choice[0][0]]
        self.assertEqual(state["label"], "Unsupported format")
        self.assertTrue(state["blocking"])
        self.retained(item, status="failed")
        self.assertEqual(
            source_readiness(self.case, "native_text", choice)[choice[0][0]]["label"],
            "Unsupported format",
        )

    def test_unsupported_method_options_do_not_prove_unsupported_format(self):
        item = self.document()
        run = self.retained(item, status="unsupported")
        key = f"document:{item.pk}"
        state = source_readiness(self.case, "native_text", [(key, item.title)])[key]
        self.assertEqual(state["label"], "Needs review")
        self.assertFalse(state["blocking"])
        self.assertEqual(
            state["url"], reverse("core:analysis:detail", args=[self.case.pk, run.pk])
        )

    def test_exact_attachment_checks_and_transcript_next_actions(self):
        call = PhoneCallEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic call",
            sender=self.person,
            recipient=self.person,
        )
        key = f"phone-call:{call.pk}"
        empty = source_readiness(self.case, "entities", [(key, call.title)])[key]
        self.assertEqual(
            empty["url"],
            reverse("phone:add_attachment", args=[self.case.pk, "phone-call", call.pk]),
        )
        recording = CallEvidenceAttachment.objects.create(
            case=self.case,
            phone_call=call,
            uploaded_by=self.users["owner"],
            file=ContentFile(b"unsupported audio", name="audio.bin"),
        )
        recording_key = key + f":{recording.pk}"
        state = source_readiness(self.case, "entities", [(key, call.title)])[key]
        self.assertEqual(
            parse_qs(urlsplit(state["url"]).query),
            {"operation": ["transcription"], "source": [recording_key]},
        )
        self.retained(
            call,
            operation="audio_metadata",
            kind="phone-call",
            attachment=recording,
            status="unsupported",
            reason="unsupported_audio_format",
        )
        state = source_readiness(
            self.case, "audio_metadata", [(recording_key, "Recording")]
        )[recording_key]
        self.assertEqual(state["label"], "Unsupported format")
        transcript = CallEvidenceAttachment.objects.create(
            case=self.case,
            phone_call=call,
            uploaded_by=self.users["owner"],
            transcript="Supplied exact text",
        )
        transcript_key = key + f":{transcript.pk}"
        states = source_readiness(
            self.case, "entities", [(key, "Call"), (transcript_key, "Transcript")]
        )
        self.assertEqual(states[transcript_key]["label"], "Ready")
        self.assertEqual(states[key]["label"], "Choose supplied transcript")
        self.assertEqual(
            parse_qs(urlsplit(states[key]["url"]).query)["source"], [transcript_key]
        )

    def test_extraction_versions_and_preparation_links_preserve_exact_selection(self):
        item = self.document()
        key = f"document:{item.pk}"
        before = source_readiness(self.case, "entities", [(key, "Document")])[key]
        self.assertEqual(before["label"], "Needs extraction")
        self.login("owner")
        response = self.client.get(before["url"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"]["sources"].value(), [key])
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "native_text",
            [{"kind": "document", "id": str(item.pk)}],
        )
        process_run(run.pk)
        run.refresh_from_db()
        extracted = f"document:{item.pk}:-:{run.result.pk}"
        form = analysis_form(case=self.case, operation="entities")
        self.assertEqual(form.readiness[extracted]["label"], "Ready")
        self.assertEqual(form.readiness[key]["label"], "Choose extracted text")
        response = self.client.get(form.readiness[key]["url"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"]["sources"].value(), [extracted])
        self.assertEqual(response.context["source_options"][-1][2]["label"], "Ready")

    def test_no_storage_or_network_probes_and_foreign_focus_rejected(self):
        item = self.document()
        foreign = self.document(case=self.other_case)
        self.login("owner")
        url = reverse("core:analysis:create", args=[self.case.pk])
        with (
            patch("core.parsers.run_parser", side_effect=AssertionError("no parser")),
            patch(
                "core.analysis.local.build_opener",
                side_effect=AssertionError("no HTTP"),
            ),
            patch(
                "django.core.files.storage.FileSystemStorage.open",
                side_effect=AssertionError("no file"),
            ),
        ):
            response = self.client.get(
                url, {"operation": "native_text", "source": f"document:{item.pk}"}
            )
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Not yet inspected")
            self.assertEqual(configuration_readiness("native_text"), "")
            self.assertIn("not configured", configuration_readiness("content_events"))
        for source in (
            f"document:{foreign.pk}",
            "document:bad",
            "bad",
            f"audio:{item.pk}",
        ):
            self.assertEqual(
                self.client.get(
                    url, {"operation": "native_text", "source": source}
                ).status_code,
                404,
            )

    def test_blank_text_and_unknown_coordinates_cannot_submit_as_ready(self):
        text = self.text()
        location = LocationEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Reported address",
            address="Synthetic address",
        )
        state = source_readiness(
            self.case, "proximity", [(f"location:{location.pk}", "Address")]
        )[f"location:{location.pk}"]
        self.assertEqual(state["label"], "Needs accepted location")
        self.assertEqual(
            parse_qs(urlsplit(state["url"]).query)["source"],
            [f"location:{location.pk}"],
        )
        document = self.document()
        form = analysis_form(
            {
                "operation": "entities",
                "submission_id": uuid4(),
                "sources": [f"text:{text.pk}", f"document:{document.pk}"],
            },
            case=self.case,
            operation="entities",
        )
        self.assertFalse(form.is_valid())
        self.assertIn("No sources were dropped", str(form.errors))
