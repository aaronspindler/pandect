"""Investigator routes enforce current case access for retained analysis work."""

import io
import json
import wave
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from django.core.files.base import ContentFile
from django.test import Client, TestCase
from django.urls import reverse

from audio.models import AudioEvidence
from cases.models import CaseMembership
from core.analysis.form_base import source_choices
from core.analysis.navigation import finding_url
from core.analysis.services import process_run, start_run
from core.analysis.testing import image_bytes
from core.models import AnalysisResult, AnalysisReview, AnalysisRun
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from image.models import ImageEvidence
from people.models import Person
from phone.models import CallEvidenceAttachment, PhoneCallEvidence, TextMessageEvidence
from text.models import TextEvidence
from vehicle.models import Vehicle


def wav_bytes():
    stream = io.BytesIO()
    with wave.open(stream, "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(8000)
        recording.writeframes(b"\x00\x00" * 800)
    return stream.getvalue()


class AnalysisViewTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.note = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Selected <script>source</script>",
            body="Write to source@example.test. <script>alert('source')</script>",
        )
        self.foreign = TextEvidence.objects.create(
            case=self.other_case,
            uploaded_by=self.users["outsider"],
            title="Private foreign source",
            body="hidden@example.test",
        )

    def test_empty_supported_source_selection_offers_a_useful_next_action(self):
        self.login("owner")
        response = self.client.get(
            self.analysis_url("create"), {"operation": "native_text"}
        )
        self.assertContains(response, "No suitable sources")
        self.assertContains(
            response, reverse("core:evidence:create", kwargs={"case_id": self.case.pk})
        )
        self.assertContains(response, 'type="submit" disabled>Queue analysis')

    def test_history_names_current_sources_without_exposing_foreign_case_titles(self):
        own = self.analysis_run(complete=False)
        foreign, _ = start_run(
            self.users["outsider"],
            self.other_case.pk,
            "entities",
            [{"kind": "text", "id": str(self.foreign.pk)}],
        )
        # Inconsistent historical references must not reveal another case's title.
        AnalysisRun.objects.filter(pk=foreign.pk).update(case=self.case)
        self.login("owner")
        response = self.client.get(self.analysis_url("index"))
        self.assertContains(response, "Selected &lt;script&gt;source&lt;/script&gt;")
        self.assertNotContains(response, self.foreign.title)
        labels = {run.pk: run.source_title for run in response.context["page_obj"]}
        self.assertEqual(labels[own.pk], self.note.title)
        self.assertEqual(labels[foreign.pk], "Source unavailable")

    def test_title_filter_empty_state_can_be_cleared_without_losing_operation(self):
        self.login("owner")
        response = self.client.get(
            self.analysis_url("create"),
            {"operation": "entities", "q": "Unmatched synthetic source"},
        )
        self.assertContains(response, "No suitable sources")
        self.assertContains(response, 'href="?operation=entities"')
        self.assertNotContains(response, self.foreign.title)

    def analysis_url(self, route, *, run=None, case=None, **extra):
        kwargs = {"case_id": (case or self.case).pk, **extra}
        if run is not None:
            kwargs["run_id"] = run.pk
        return reverse(f"core:analysis:{route}", kwargs=kwargs)

    def payload(self, **values):
        return {
            "operation": "entities",
            "submission_id": str(uuid4()),
            "sources": [f"text:{self.note.pk}"],
        } | values

    def analysis_run(
        self, *, complete=True, actor="owner", source=None, operation="entities"
    ):
        item = source or self.note
        run, _ = start_run(
            self.users[actor],
            self.case.pk,
            operation,
            [{"kind": "text", "id": str(item.pk)}],
        )
        if complete:
            process_run(run.pk)
            run.refresh_from_db()
            self.assertEqual(run.status, "complete", run.error_code)
        return run

    def assert_private(self, response):
        self.assertIn("private", response["Cache-Control"])
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_read_routes_roles_superusers_and_revocation(self):
        run = self.analysis_run()
        paths = [
            self.analysis_url("index"),
            self.analysis_url("detail", run=run),
            self.analysis_url("download", run=run),
            self.analysis_url("report", name="calls"),
        ]
        for role, expected in (
            ("owner", 200),
            ("editor", 200),
            ("viewer", 200),
            ("outsider", 404),
        ):
            self.login(role)
            for path in paths:
                with self.subTest(role=role, path=path):
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, expected)
                    if expected == 200:
                        self.assert_private(response)
        self.users["outsider"].is_superuser = True
        self.users["outsider"].save()
        self.login("outsider")
        self.assertEqual(self.client.get(paths[1]).status_code, 404)
        self.login("viewer")
        CaseMembership.objects.filter(
            case=self.case, user=self.users["viewer"]
        ).delete()
        self.assertEqual(self.client.get(paths[2]).status_code, 404)

    def test_create_is_current_editor_operation_and_picker_is_scoped(self):
        for role, expected in (
            ("owner", 200),
            ("editor", 200),
            ("viewer", 403),
            ("outsider", 404),
        ):
            self.login(role)
            response = self.client.get(
                self.analysis_url("create"), {"operation": "entities"}
            )
            self.assertEqual(response.status_code, expected)
            if expected == 200:
                self.assertNotContains(response, "source@example.test")
                self.assertContains(
                    response, "Selected &lt;script&gt;source&lt;/script&gt;"
                )
                self.assertNotContains(response, self.foreign.title)
                self.assert_private(response)
        choices = source_choices(self.case, "entities")
        self.assertIn(f"text:{self.note.pk}", [value for value, label in choices])
        self.assertNotIn(f"text:{self.foreign.pk}", [value for value, label in choices])

    def test_evidence_analysis_is_readable_but_only_editors_can_start_it(self):
        url = self.analysis_url(
            "process_evidence", kind="text", evidence_id=self.note.pk
        )
        self.assertEqual(self.client.get(url).status_code, 302)
        for role, expected in (
            ("owner", 200),
            ("editor", 200),
            ("viewer", 200),
            ("outsider", 404),
        ):
            self.login(role)
            with self.subTest(role=role):
                detail = self.client.get(self.url("detail", self.note))
                response = self.client.get(url)
                self.assertEqual(response.status_code, expected)
                if expected == 200:
                    self.assert_private(response)
                    self.assertContains(detail, f'href="{url}"')
                    self.assertContains(response, 'aria-current="page">Analysis</a>')
                    if role == "viewer":
                        self.assertNotContains(response, "External analysis")
                        self.assertNotContains(response, ">Analyze evidence</button>")
                    else:
                        self.assertContains(response, ">Analyze evidence</button>")
                if role in {"viewer", "outsider"}:
                    self.assertEqual(
                        self.client.post(url, self.payload()).status_code,
                        403 if role == "viewer" else 404,
                    )
        self.assertFalse(AnalysisRun.objects.exists())
        self.login("editor")
        CaseMembership.objects.filter(
            case=self.case, user=self.users["editor"]
        ).delete()
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, self.payload()).status_code, 404)

    def test_evidence_analysis_retains_collection_filters_separately_from_results_page(
        self,
    ):
        self.login("editor")
        url = self.analysis_url(
            "process_evidence", kind="text", evidence_id=self.note.pk
        )
        suffix = "?q=Selected&type=text&state=all&page=2"
        response = self.client.get(url + suffix + "&results_page=99")
        self.assertEqual(response.context["page_obj"].number, 1)
        self.assertContains(response, f'href="{url}{suffix.replace("&", "&amp;")}"')
        original = self.url("detail", self.note) + suffix
        self.assertContains(response, f'href="{original.replace("&", "&amp;")}"')
        self.assertRedirects(
            self.client.post(url + suffix, {"submission_id": str(uuid4())}),
            url + suffix,
        )

    def test_evidence_analysis_uses_url_source_and_ignores_forged_operation_settings(
        self,
    ):
        other = self.text()
        self.login("owner")
        url = self.analysis_url(
            "process_evidence", kind="text", evidence_id=self.note.pk
        )
        response = self.client.get(url)
        self.assertEqual(set(response.context["form"].fields), {"submission_id"})
        self.assertNotContains(response, "Find source titles")
        self.assertNotContains(response, self.foreign.title)
        self.assertNotContains(response, other.title)
        payload = self.payload(
            operation="content_events",
            sources=[f"text:{self.foreign.pk}", f"text:{other.pk}"],
            external_processing="on",
            include_summary="on",
        )
        self.assertRedirects(self.client.post(url, payload), url)
        runs = list(AnalysisRun.objects.all())
        self.assertEqual(
            {run.operation for run in runs},
            {"entities", "text_topics"},
        )
        for run in runs:
            self.assertFalse(run.external_processing)
            self.assertEqual(
                [selected["evidence_id"] for selected in run.inputs],
                [str(self.note.pk)],
            )
            if run.operation == "text_topics":
                self.assertFalse(run.parameters["include_summary"])
        for kind, item in (("text", self.foreign), ("image", self.note)):
            target = self.analysis_url(
                "process_evidence", kind=kind, evidence_id=item.pk
            )
            self.assertEqual(self.client.get(target).status_code, 404)
            self.assertEqual(self.client.post(target, self.payload()).status_code, 404)

    def test_process_archived_evidence_queues_once_and_preserves_original(self):
        self.login("editor")
        self.assertEqual(
            self.client.post(self.url("archive", self.note)).status_code, 302
        )
        self.note.refresh_from_db()
        original = (self.note.body, self.note.sha256)
        url = self.analysis_url(
            "process_evidence", kind="text", evidence_id=self.note.pk
        )
        self.assertContains(self.client.get(url), "Archived")
        payload = {"submission_id": str(uuid4())}
        first = self.client.post(url, payload)
        first_ids = set(AnalysisRun.objects.values_list("pk", flat=True))
        self.assertEqual(len(first_ids), 2)
        repeated = self.client.post(url, payload)
        self.assertRedirects(first, url)
        self.assertEqual(first["Location"], repeated["Location"])
        self.assertEqual(
            set(AnalysisRun.objects.values_list("pk", flat=True)), first_ids
        )
        run = AnalysisRun.objects.get(operation="entities")
        process_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "complete", run.error_code)
        self.note.refresh_from_db()
        self.assertEqual((self.note.body, self.note.sha256), original)

    def test_evidence_analysis_validation_csrf_and_history_are_scoped(self):
        self.login("owner")
        selected = self.analysis_run()
        other = self.analysis_run(source=self.text(), complete=False)
        url = self.analysis_url(
            "process_evidence", kind="text", evidence_id=self.note.pk
        )
        response = self.client.get(url)
        self.assertEqual(
            [run.pk for run in response.context["page_obj"]], [selected.pk]
        )
        self.assertNotContains(response, self.analysis_url("detail", run=other))
        invalid = self.client.post(url, {"submission_id": "invalid"})
        self.assertContains(invalid, "Analysis could not start", status_code=400)
        self.assertEqual(AnalysisRun.objects.count(), 2)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.users["owner"])
        self.assertEqual(csrf_client.post(url, self.payload()).status_code, 403)

    def test_evidence_highlights_use_latest_result_and_current_finding_reviews(self):
        earlier = self.analysis_run()
        other = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Other retained note",
            body="Contact other@example.test.",
        )
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "entities",
            [
                {"kind": "text", "id": str(self.note.pk)},
                {"kind": "text", "id": str(other.pk)},
            ],
        )
        process_run(run.pk)
        result = AnalysisResult.objects.get(run=run)
        for decision in ("rejected", "accepted"):
            AnalysisReview.objects.create(
                case=self.case,
                result=result,
                item_index=0,
                reviewed_by=self.users["owner"],
                decision=decision,
            )
        # Fill the first history page with newer queued runs.
        AnalysisRun.objects.bulk_create(
            [
                AnalysisRun(
                    case=self.case,
                    requested_by=self.users["owner"],
                    operation=run.operation,
                    method_version=run.method_version,
                    inputs=run.inputs,
                )
                for _ in range(25)
            ]
        )
        self.login("viewer")
        url = self.analysis_url(
            "process_evidence", kind="text", evidence_id=self.note.pk
        )
        response = self.client.get(url)
        self.assertNotContains(
            response, f'href="{self.analysis_url("detail", run=run)}"'
        )
        self.assertContains(response, "source@example.test", count=1)
        self.assertNotContains(response, "other@example.test")
        self.assertContains(response, "Accepted")
        target = finding_url(self.case.pk, run.pk, 0)
        self.assertContains(response, target.replace("&", "&amp;"))
        self.assertContains(self.client.get(target), "source@example.test")
        second_page = self.client.get(url, {"results_page": 2})
        self.assertContains(second_page, "source@example.test", count=1)
        self.assertContains(second_page, self.analysis_url("detail", run=earlier))
        self.assertContains(second_page, target.replace("&", "&amp;"))

        AnalysisReview.objects.create(
            case=self.case,
            result=result,
            item_index=0,
            reviewed_by=self.users["owner"],
            decision="rejected",
        )
        response = self.client.get(url)
        self.assertNotContains(response, "source@example.test")
        self.assertNotContains(response, target.replace("&", "&amp;"))

    def test_evidence_highlights_show_embedded_capture_time_until_source_changes(self):
        image = ImageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic photo",
            file=ContentFile(image_bytes(exif=True), name="synthetic.jpeg"),
        )
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "image_metadata",
            [{"kind": "image", "id": str(image.pk)}],
        )
        process_run(run.pk)
        self.login("owner")
        url = self.analysis_url("process_evidence", kind="image", evidence_id=image.pk)
        response = self.client.get(url)
        self.assertContains(response, "Capture time candidate")
        self.assertContains(response, "2026-10-27T12:34:56")
        self.assertNotContains(response, "Input SHA-256")
        self.assertNotContains(response, "GPS candidate")

        ImageEvidence.objects.filter(pk=image.pk).update(sha256="a" * 64)
        response = self.client.get(url)
        self.assertNotContains(response, "2026-10-27T12:34:56")
        self.assertContains(response, self.analysis_url("detail", run=run))

    def test_external_evidence_picker_preselects_target_beyond_case_choice_limit(self):
        TextEvidence.objects.bulk_create(
            [
                TextEvidence(
                    case=self.case,
                    uploaded_by=self.users["owner"],
                    title=f"Other retained note {index}",
                    body="Synthetic other evidence.",
                    sha256="a" * 64,
                )
                for index in range(501)
            ]
        )
        self.login("owner")
        response = self.client.get(
            self.analysis_url(
                "external_evidence", kind="text", evidence_id=self.note.pk
            ),
            {"q": "Does not match the target"},
        )
        form = response.context["form"]
        self.assertEqual(form["sources"].value(), [f"text:{self.note.pk}"])
        self.assertEqual(len(form.fields["sources"].choices), 1)
        self.assertEqual(
            dict(form.fields["operation"].choices),
            {
                "content_events": "Suggest claims and commitments",
                "text_topics": "Inspect keywords and topics",
            },
        )
        self.assertNotContains(response, "Other retained note")
        self.assertNotContains(response, self.foreign.title)
        self.assertNotContains(response, "Find source titles")

    def test_external_evidence_requires_approval_current_edit_access_and_csrf(self):
        url = self.analysis_url(
            "external_evidence", kind="text", evidence_id=self.note.pk
        )
        payload = self.payload(operation="content_events", backend="gateway")
        for role, expected in (("viewer", 403), ("outsider", 404)):
            self.login(role)
            self.assertEqual(self.client.get(url).status_code, expected)
            self.assertEqual(self.client.post(url, payload).status_code, expected)
        self.login("owner")
        response = self.client.post(url, payload)
        self.assertIn("external_processing", response.context["form"].errors)
        self.assertFalse(AnalysisRun.objects.exists())
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.users["owner"])
        self.assertEqual(csrf_client.post(url, payload).status_code, 403)
        self.assertEqual(
            self.client.post(url, payload | {"external_processing": "on"}).status_code,
            302,
        )
        self.assertTrue(AnalysisRun.objects.get().external_processing)

    def test_external_evidence_rejects_other_sources_and_local_operations(self):
        other = self.text()
        self.login("owner")
        url = self.analysis_url(
            "external_evidence", kind="text", evidence_id=self.note.pk
        )
        for values in (
            {"sources": [f"text:{other.pk}"]},
            {"sources": [f"text:{self.foreign.pk}"]},
            {"sources": [f"text:{self.note.pk}", f"text:{other.pk}"]},
            {"operation": "entities"},
        ):
            with self.subTest(values=values):
                response = self.client.post(
                    url,
                    self.payload(operation="content_events", external_processing="on")
                    | values,
                )
                self.assertTrue(response.context["form"].errors)
                self.assertContains(response, "Review your selection")
        self.assertFalse(AnalysisRun.objects.exists())

    def test_external_evidence_retained_extraction_and_call_additions_stay_scoped(self):
        document = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic extraction source",
            file=ContentFile(b"Write to extracted@example.test", name="source.txt"),
        )
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "native_text",
            [{"kind": "document", "id": str(document.pk)}],
        )
        process_run(run.pk)
        result = AnalysisResult.objects.get(run=run)
        self.login("owner")
        url = self.analysis_url(
            "external_evidence", kind="document", evidence_id=document.pk
        )
        derived = f"document:{document.pk}:-:{result.pk}"
        response = self.client.get(url, {"operation": "content_events"})
        self.assertIn(derived, dict(response.context["form"].fields["sources"].choices))
        call = PhoneCallEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic call",
            sender=self.person,
            recipient=self.person,
        )
        attachment = CallEvidenceAttachment.objects.create(
            case=self.case,
            phone_call=call,
            uploaded_by=self.users["owner"],
            file=ContentFile(wav_bytes(), name="call.wav"),
        )
        response = self.client.get(
            self.analysis_url(
                "external_evidence", kind="phone-call", evidence_id=call.pk
            )
        )
        self.assertEqual(
            response.context["form"]["sources"].value(),
            [f"phone-call:{call.pk}:{attachment.pk}"],
        )
        empty_call = PhoneCallEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Metadata only",
            sender=self.person,
            recipient=self.person,
        )
        url = self.analysis_url(
            "external_evidence", kind="phone-call", evidence_id=empty_call.pk
        )
        response = self.client.get(url)
        self.assertContains(response, "No suitable sources")
        self.assertContains(response, 'type="submit" disabled>Queue analysis')
        self.assertNotContains(response, "call.wav")
        response = self.client.post(
            self.analysis_url(
                "process_evidence", kind="phone-call", evidence_id=empty_call.pk
            ),
            {"submission_id": str(uuid4())},
            follow=True,
        )
        self.assertContains(response, "No local analysis is available")
        self.assertEqual(AnalysisRun.objects.count(), 1)

    def test_start_result_review_download_and_escaped_findings(self):
        self.login("editor")
        response = self.client.post(self.analysis_url("create"), self.payload())
        self.assertEqual(response.status_code, 302)
        run = AnalysisRun.objects.get()
        self.assertEqual(run.status, "queued")
        self.assertEqual(run.requested_by, self.users["editor"])
        self.assertEqual(AnalysisResult.objects.count(), 0)
        process_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "complete", run.error_code)
        response = self.client.get(self.analysis_url("detail", run=run))
        self.assertContains(response, "source@example.test")
        self.assertContains(response, "Selected &lt;script&gt;source&lt;/script&gt;")
        self.assertNotContains(response, "<script>source</script>")
        self.assertContains(response, "Characters")
        self.assertContains(
            response,
            reverse(
                "core:evidence:detail",
                kwargs={
                    "case_id": self.case.pk,
                    "kind": "text",
                    "evidence_id": self.note.pk,
                },
            ),
        )
        self.assert_private(response)
        response = self.client.post(
            self.analysis_url("review", run=run),
            {"decision": "accepted", "comment": "<script>review</script>"},
        )
        self.assertEqual(response.status_code, 302)
        review = AnalysisReview.objects.get()
        self.assertEqual(review.reviewed_by, self.users["editor"])
        response = self.client.get(self.analysis_url("detail", run=run))
        self.assertContains(response, "&lt;script&gt;review&lt;/script&gt;")
        response = self.client.get(self.analysis_url("download", run=run))
        data = json.loads(response.content)
        self.assertEqual(data["case_id"], str(self.case.pk))
        self.assertEqual(data["inputs"][0]["sha256"], self.note.sha256)
        self.assertEqual(data["result"]["items"][0]["text"], "source@example.test")
        self.assertEqual(response["Content-Type"], "application/octet-stream")
        self.assertEqual(
            response["Content-Disposition"],
            f'attachment; filename="analysis-{run.pk}.json"',
        )
        self.assert_private(response)
        self.note.refresh_from_db()
        self.assertIn("<script>", self.note.body)

    def test_viewer_cannot_start_review_cancel_or_retry(self):
        run = self.analysis_run()
        queued = self.analysis_run(complete=False)
        self.login("viewer")
        for name, target, payload in (
            ("create", None, self.payload()),
            ("review", run, {"decision": "accepted"}),
            ("cancel", queued, {}),
            ("retry", run, {"submission_id": str(uuid4())}),
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    self.client.post(
                        self.analysis_url(name, run=target), payload
                    ).status_code,
                    403,
                )
        self.assertEqual(AnalysisReview.objects.count(), 0)
        self.assertEqual(AnalysisRun.objects.count(), 2)
        response = self.client.get(self.analysis_url("detail", run=run))
        self.assertNotContains(response, "Record review")
        self.assertNotContains(response, "Start another run with these inputs")

    def test_invalid_review_preserves_input_for_correction_and_retry(self):
        run = self.analysis_run()
        self.login("editor")
        url = self.analysis_url("review", run=run)
        comment = "<script>Retained review wording</script>" + "x" * 4000
        response = self.client.post(url, {"decision": "accepted", "comment": comment})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.context["form"]["comment"].value(), comment)
        self.assertContains(response, "data-error-summary", status_code=400)
        self.assertContains(
            response, "&lt;script&gt;Retained review wording", status_code=400
        )
        self.assertNotContains(response, 'name="item_index"', status_code=400)
        self.assertEqual(AnalysisReview.objects.count(), 0)
        self.assert_private(response)
        corrected = "  Source checked; uncertainty remains.\n"
        response = self.client.post(url, {"decision": "accepted", "comment": corrected})
        self.assertRedirects(response, self.analysis_url("detail", run=run))
        saved = AnalysisReview.objects.get()
        self.assertEqual(saved.comment, corrected)
        self.assertIsNone(saved.item_index)

    def test_crosscase_run_source_and_relation_parameters_fail_closed(self):
        run = self.analysis_run()
        self.login("outsider")
        for name in ("detail", "download", "review", "cancel", "retry"):
            method = (
                self.client.get if name in {"detail", "download"} else self.client.post
            )
            self.assertEqual(
                method(
                    self.analysis_url(name, run=run, case=self.other_case)
                ).status_code,
                404,
            )
        self.login("owner")
        response = self.client.post(
            self.analysis_url("create"),
            self.payload(sources=[f"text:{self.foreign.pk}"]),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Review your selection")
        self.assertEqual(AnalysisRun.objects.count(), 1)
        foreign_vehicle = Vehicle.objects.create(case=self.other_case, model="Foreign")
        response = self.client.get(
            self.analysis_url("report", name="vehicle-coverage"),
            {"state": "all", "vehicle_id": str(foreign_vehicle.pk)},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Select a valid choice")
        self.assertNotContains(response, foreign_vehicle.model)

    def test_csrf_and_post_only_mutations(self):
        queued = self.analysis_run(complete=False)
        self.login("owner")
        for name in ("cancel", "retry", "review"):
            self.assertEqual(
                self.client.get(self.analysis_url(name, run=queued)).status_code, 405
            )
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.users["owner"])
        self.assertEqual(
            client.post(self.analysis_url("create"), self.payload()).status_code, 403
        )
        client.get(self.analysis_url("create"), {"operation": "entities"})
        data = self.payload() | {
            "csrfmiddlewaretoken": client.cookies["csrftoken"].value
        }
        self.assertEqual(
            client.post(self.analysis_url("create"), data).status_code, 302
        )

    def test_cancel_retry_and_submission_recovery_preserve_history(self):
        self.login("owner")
        data = self.payload()
        self.assertEqual(
            self.client.post(self.analysis_url("create"), data).status_code, 302
        )
        original = AnalysisRun.objects.get()
        self.assertEqual(
            self.client.post(self.analysis_url("create"), data).status_code, 302
        )
        self.assertEqual(AnalysisRun.objects.count(), 1)
        self.assertEqual(
            self.client.post(self.analysis_url("cancel", run=original)).status_code, 302
        )
        original.refresh_from_db()
        self.assertEqual(original.status, "cancelled")
        process_run(original.pk)
        self.assertFalse(AnalysisResult.objects.filter(run=original).exists())
        retry_id = str(uuid4())
        self.assertEqual(
            self.client.post(
                self.analysis_url("retry", run=original), {"submission_id": retry_id}
            ).status_code,
            302,
        )
        retry = AnalysisRun.objects.exclude(pk=original.pk).get()
        self.assertEqual(retry.previous_run, original)
        self.assertEqual(retry.inputs, original.inputs)
        self.assertEqual(
            self.client.post(
                self.analysis_url("retry", run=original), {"submission_id": retry_id}
            ).status_code,
            302,
        )
        self.assertEqual(AnalysisRun.objects.count(), 2)

    def test_revocation_and_downgrade_before_publication_blocks_result(self):
        self.login("editor")
        response = self.client.post(self.analysis_url("create"), self.payload())
        self.assertEqual(response.status_code, 302)
        run = AnalysisRun.objects.get()
        CaseMembership.objects.filter(case=self.case, user=self.users["editor"]).update(
            role="viewer"
        )
        process_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error_code, "access_removed")
        self.assertFalse(AnalysisResult.objects.filter(run=run).exists())

    def test_stale_source_withholds_result_and_download(self):
        run = self.analysis_run()
        TextEvidence.objects.filter(pk=self.note.pk).update(
            body="Changed via trusted maintenance"
        )
        self.login("owner")
        response = self.client.get(self.analysis_url("detail", run=run))
        self.assertContains(response, "result is withheld")
        self.assertNotContains(response, "source@example.test")
        self.assertEqual(
            self.client.get(self.analysis_url("download", run=run)).status_code, 404
        )

    def test_report_filters_unknown_durations_archive_and_empty_state(self):
        stamp = datetime(2026, 10, 27, 12, tzinfo=UTC)
        for title, duration, archived in (
            ("Unknown duration", None, False),
            ("Real zero duration", timedelta(), False),
            ("Archived duration", timedelta(seconds=90), True),
        ):
            PhoneCallEvidence.objects.create(
                case=self.case,
                uploaded_by=self.users["owner"],
                title=title,
                sender=self.person,
                recipient=self.person,
                occurred_at=stamp,
                duration=duration,
                is_archived=archived,
                archive_changed_at=stamp if archived else None,
                archive_changed_by=self.users["owner"] if archived else None,
            )
        self.login("viewer")
        url = self.analysis_url("report", name="calls")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Unknown duration")
        self.assertContains(response, "Real zero duration")
        self.assertNotContains(response, "Archived duration")
        response = self.client.get(url, {"state": "archived"})
        self.assertContains(response, "Archived duration")
        self.assertNotContains(response, "Real zero duration")
        response = self.client.get(url, {"state": "all", "start": "2026-10-28T00:00"})
        self.assertContains(response, "No records in this selection")
        response = self.client.get(
            url,
            {"state": "all", "start": "2026-10-29T00:00", "end": "2026-10-28T00:00"},
        )
        self.assertContains(response, "The end must follow the start")
        self.assertEqual(
            self.client.get(self.analysis_url("report", name="unknown")).status_code,
            404,
        )

    def test_standalone_and_call_recordings_have_private_scoped_inline_access(self):
        recording = AudioEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Standalone recording",
            file=ContentFile(wav_bytes(), name="recording.wav"),
        )
        call = PhoneCallEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Retained call",
            sender=self.person,
            recipient=self.person,
        )
        addition = CallEvidenceAttachment.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            phone_call=call,
            file=ContentFile(wav_bytes(), name="call.wav"),
        )
        for choice in (
            {"kind": "audio", "id": str(recording.pk)},
            {
                "kind": "phone-call",
                "id": str(call.pk),
                "attachment_id": str(addition.pk),
            },
        ):
            with self.subTest(choice=choice):
                run, _ = start_run(
                    self.users["owner"], self.case.pk, "audio_metadata", [choice]
                )
                source_id = run.inputs[0]["id"]
                url = self.analysis_url("audio", run=run, source_id=source_id)
                self.login("viewer")
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], "audio/wav")
                self.assert_private(response)
                self.assertEqual(b"".join(response.streaming_content), wav_bytes())
                self.login("outsider")
                self.assertEqual(self.client.get(url).status_code, 404)
                self.login("owner")
                self.assertEqual(
                    self.client.get(
                        self.analysis_url("audio", run=run, source_id=uuid4())
                    ).status_code,
                    404,
                )
        choices = source_choices(self.case, "audio_metadata")
        self.assertTrue(
            any(value.endswith(str(addition.pk)) for value, label in choices)
        )

    def test_import_confirmation_exposes_native_context_through_private_message(self):
        account = "Account <unsafe>"
        html = (
            '<div class="message" data-message-id="native-1" '
            'data-thread-id="native-thread"><span class="timestamp">'
            '2026-10-27T12:00:00Z</span><span class="sender">'
            'Account &lt;unsafe&gt;</span><span class="bubble">'
            "A proposed meeting.</span></div>"
        )
        document = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Retained message export",
            file=ContentFile(html.encode(), name="export.html"),
        )
        other = Person.objects.create(case=self.case, first_name="Explicit recipient")
        self.login("owner")
        payload = self.payload(
            operation="conversation_import",
            sources=[f"document:{document.pk}"],
            me_label=account,
            them_label="Other account",
            me=str(self.person.pk),
            them=str(other.pk),
            timezone="UTC",
        )
        self.assertEqual(
            self.client.post(self.analysis_url("create"), payload).status_code, 302
        )
        run = AnalysisRun.objects.get()
        process_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "complete", run.error_code)
        message = TextMessageEvidence.objects.get()
        self.login("viewer")
        url = reverse(
            "core:evidence:detail",
            kwargs={
                "case_id": self.case.pk,
                "kind": "text-message",
                "evidence_id": message.pk,
            },
        )
        response = self.client.get(url)
        self.assertContains(response, "Imported source context")
        self.assertContains(response, "native-1")
        self.assertContains(response, "native-thread")
        self.assertContains(response, "Account &lt;unsafe&gt;")
        self.assertNotContains(response, "Account <unsafe>")
        self.assertContains(response, "Retained message export")
        self.assert_private(response)
        self.login("outsider")
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_navigation_and_queued_progress_remain_available_without_ai(self):
        run = self.analysis_run(complete=False)
        self.login("owner")
        response = self.client.get(
            reverse("core:evidence:list", kwargs={"case_id": self.case.pk})
        )
        self.assertContains(response, self.analysis_url("index"))
        response = self.client.get(self.analysis_url("detail", run=run))
        self.assertContains(response, "Queued")
        self.assertContains(response, "Refresh status")
        self.assertContains(response, "Cancel processing")
        self.assertNotContains(response, "Download results")
