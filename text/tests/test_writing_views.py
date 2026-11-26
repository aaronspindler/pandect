from datetime import UTC, datetime
from uuid import uuid4

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from cases.models import CaseMembership
from core.analysis.sources import source_id
from core.analysis.worker import process_run
from core.models import AnalysisRun
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from people.models import Person
from phone.models import CommunicationContext, TextMessageEvidence


class PersonWritingViewTests(EvidenceViewTestMixin, TestCase):
    periods = {
        "first_start": "2026-04-01",
        "first_end": "2026-04-01",
        "second_start": "2026-04-03",
        "second_end": "2026-04-03",
    }

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.peer = Person.objects.create(case=cls.case, first_name="Synthetic peer")
        cls.foreign = Person.objects.create(
            case=cls.other_case, first_name="Foreign person"
        )

    def writing_url(self, *, compare=False, person=None):
        return reverse(
            "people:writing_style",
            args=[self.case.pk, (person or self.person).pk],
            query={"mode": "compare"} if compare else None,
        )

    def message(self, **values):
        return TextMessageEvidence.objects.create(
            **(
                {
                    "case": self.case,
                    "uploaded_by": self.users["owner"],
                    "title": "Synthetic message",
                    "sender": self.person,
                    "recipient": self.peer,
                    "body": (
                        "Hello Jordan. The river path is clear after yesterday's "
                        "rain. We can take the footbridge past the market on our "
                        "way to the station. Please bring your camera, a spare "
                        "battery, and the printed route for each stop. Thanks!"
                    ),
                }
                | values
            )
        )

    def post(self, *, compare=False, **payload):
        return self.client.post(
            self.writing_url(compare=compare),
            {"submission_id": str(uuid4())} | payload,
        )

    def test_profile_uses_active_attributed_sources_and_retains_viewer_history(self):
        dated = self.message(
            title="Dated original", occurred_at=datetime(2026, 4, 1, 12, tzinfo=UTC)
        )
        body = "The printed route is on the table."
        header = b"Sender,Text\n"
        original = header + f"sender-1,{body}\n".encode()
        export = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Retained group export",
            file=SimpleUploadedFile("group.csv", original, "text/csv"),
        )
        undated = self.message(
            title="Undated group original",
            recipient=None,
            group_source=export,
            body=body,
        )
        CommunicationContext.objects.create(
            case=self.case,
            message=undated,
            source_document=export,
            source_sha256=export.sha256,
            source_row=1,
            source_start=len(header),
            source_end=len(original),
            parser_version="imessage-csv-v1",
            timezone="UTC",
            raw_sender="sender-1",
        )
        incoming = self.message(title="Incoming original", sender=self.peer)
        archived = self.message(title="Archived original")
        TextMessageEvidence.objects.filter(pk=archived.pk).update(
            is_archived=True,
            archive_changed_at=timezone.now(),
            archive_changed_by=self.users["owner"],
        )
        foreign = self.message(
            case=self.other_case,
            uploaded_by=self.users["outsider"],
            sender=self.foreign,
            recipient=self.foreign,
        )
        self.text()
        self.login("owner")
        person_detail = reverse("people:detail", args=[self.case.pk, self.person.pk])
        self.assertContains(
            self.client.get(person_detail), f'href="{self.writing_url()}"'
        )
        submission = str(uuid4())
        selection = {
            "submission_id": submission,
            "sources": [str(incoming.pk), str(archived.pk), str(foreign.pk)],
        }
        queued = self.post(**selection)
        run = AnalysisRun.objects.get(operation="style_profiles")
        run_url = reverse("core:analysis:detail", args=[self.case.pk, run.pk])
        self.assertRedirects(queued, run_url, fetch_redirect_response=False)
        self.assertEqual(self.post(**selection).url, queued.url)
        self.assertEqual(AnalysisRun.objects.count(), 1)
        self.assertEqual(
            run.parameters["writing_scope"]["person_id"], str(self.person.pk)
        )
        self.assertFalse(run.external_processing)
        self.assertEqual(
            {source["evidence_id"] for source in run.inputs},
            {str(dated.pk), str(undated.pk)},
        )
        process_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, AnalysisRun.Status.COMPLETE, run.error_code)

        self.login("viewer")
        history = self.client.get(self.writing_url())
        self.assertContains(history, run_url)
        self.assertNotContains(history, "Start analysis")
        self.assertNotContains(
            self.client.get(self.writing_url(person=self.peer)), run_url
        )
        result = self.client.get(run_url)
        self.assertContains(result, dated.title)
        self.assertContains(result, undated.title)
        self.assertNotContains(result, incoming.title)
        self.assertEqual(self.post().status_code, 403)
        CaseMembership.objects.filter(
            case=self.case, user=self.users["viewer"]
        ).delete()
        self.assertEqual(self.client.get(self.writing_url()).status_code, 404)
        self.assertEqual(self.client.get(run_url).status_code, 404)

    def test_compare_uses_inclusive_local_reported_dates_and_exact_groups(self):
        self.login("editor")
        self.client.cookies["pandect_timezone"] = "America/Toronto"
        earlier = self.message(occurred_at=datetime(2026, 4, 2, 3, 59, tzinfo=UTC))
        later = self.message(occurred_at=datetime(2026, 4, 3, 4, tzinfo=UTC))
        self.message(occurred_at=datetime(2026, 4, 2, 4, tzinfo=UTC))
        self.message(collected_at=earlier.occurred_at)
        self.message(sender=self.peer, occurred_at=later.occurred_at)
        self.assertContains(
            self.client.get(self.writing_url(compare=True)), "America/Toronto"
        )
        response = self.post(compare=True, **self.periods)
        self.assertEqual(response.status_code, 302)
        run = AnalysisRun.objects.get(operation="style_change")
        self.assertEqual(run.parameters["writing_scope"]["timezone"], "America/Toronto")
        self.assertFalse(run.external_processing)
        for group, item in zip(("group_a", "group_b"), (earlier, later), strict=True):
            self.assertEqual(
                run.parameters[group],
                [source_id({"kind": "text-message", "id": item.pk})],
            )
        self.assertEqual(
            {source["evidence_id"] for source in run.inputs},
            {str(earlier.pk), str(later.pk)},
        )

        bounded = self.post(first_end=self.periods["first_end"])
        self.assertEqual(bounded.status_code, 302)
        profile = AnalysisRun.objects.get(operation="style_profiles")
        self.assertEqual(
            [source["evidence_id"] for source in profile.inputs], [str(earlier.pk)]
        )

    @override_settings(PANDECT_ANALYSIS_MAX_SOURCES=1)
    def test_invalid_empty_or_oversized_samples_do_not_queue(self):
        self.login("owner")
        self.message(occurred_at=datetime(2026, 4, 1, 12, tzinfo=UTC))
        self.message()
        for dates in (
            self.periods | {"first_end": self.periods["second_start"]},
            self.periods | {"second_end": "2026-04-02"},
            self.periods | {"second_start": ""},
            self.periods,  # The later window has no attributed source.
        ):
            with self.subTest(dates=dates):
                response = self.post(compare=True, **dates)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    response.context["form"]["first_start"].value(),
                    dates["first_start"],
                )
        self.assertEqual(self.post().status_code, 400)
        self.message(occurred_at=datetime(2026, 4, 3, 12, tzinfo=UTC))
        self.assertEqual(self.post(compare=True, **self.periods).status_code, 400)
        self.assertFalse(AnalysisRun.objects.exists())

    def test_foreign_person_and_unrelated_user_cannot_access_route(self):
        self.login("owner")
        CaseMembership.objects.create(case=self.other_case, user=self.users["owner"])
        self.assertEqual(
            self.client.get(self.writing_url(person=self.foreign)).status_code, 404
        )
        self.login("outsider")
        self.assertEqual(self.client.get(self.writing_url()).status_code, 404)
        self.assertEqual(self.post().status_code, 404)
