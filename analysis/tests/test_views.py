"""Investigator routes enforce current case access for retained analysis work."""

import io
import json
import wave
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from django.core.files.base import ContentFile
from django.test import Client, TestCase
from django.urls import reverse

from analysis.forms import source_choices
from analysis.models import AnalysisResult, AnalysisReview, AnalysisRun
from analysis.services import process_run, start_run
from cases.models import CaseMembership
from evidence.models import (
    AudioEvidence,
    CallEvidenceAttachment,
    DocumentEvidence,
    PhoneCallEvidence,
    TextEvidence,
    TextMessageEvidence,
)
from evidence.tests.test_views import EvidenceViewTestMixin
from people.models import Person
from vehicles.models import Vehicle


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
            response, reverse("evidence:create", kwargs={"case_id": self.case.pk})
        )
        self.assertContains(response, 'type="submit" disabled>Queue analysis')

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
        return reverse(f"analysis:{route}", kwargs=kwargs)

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
                "evidence:detail",
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
        self.assertIn("attachment", response["Content-Disposition"])
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
            "evidence:detail",
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
            reverse("evidence:list", kwargs={"case_id": self.case.pk})
        )
        self.assertContains(response, self.analysis_url("index"))
        response = self.client.get(self.analysis_url("detail", run=run))
        self.assertContains(response, "Queued")
        self.assertContains(response, "Refresh status")
        self.assertContains(response, "Cancel processing")
        self.assertNotContains(response, "Download result and provenance")
