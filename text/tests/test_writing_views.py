import re
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from cases.models import CaseMembership
from core.analysis.retries import retry_analysis
from core.analysis.worker import process_run
from core.models import AnalysisRun
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from people.models import Person
from phone.models import CommunicationContext, TextMessageEvidence
from text.analysis.attributed_sources import verify_membership
from text.analysis.attributed_writing import analyze_attributed_writing


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

    def test_writing_limits_bound_the_frozen_selection_and_body_bytes(self):
        self.login("owner")
        for day in (1, 2):
            self.message(
                occurred_at=datetime(2026, 4, day, 12, tzinfo=UTC),
                body="é" * 12,
            )
        with patch("text.analysis.attributed_sources.MAX_WRITING_MESSAGES", 1):
            self.assertContains(
                self.post(), "Choose up to 1 messages per analysis", status_code=400
            )
        with self.settings(PANDECT_ANALYSIS_MAX_BYTES=30):
            self.assertContains(
                self.post(), "exceed the writing analysis size limit", status_code=400
            )
            self.assertFalse(
                AnalysisRun.objects.filter(operation="style_profiles").exists()
            )
            self.assertEqual(self.post(first_end="2026-04-01").status_code, 302)

    def test_date_filters_exclude_imported_messages_with_only_a_calendar_date(self):
        self.login("owner")
        self.client.cookies["pandect_timezone"] = "UTC"
        stamp = datetime(2026, 4, 1, tzinfo=UTC)
        timed = self.message(occurred_at=stamp)
        date_only = self.message(occurred_at=stamp)
        data = b"Sender,Date\nsender-1,2026-04-01\n"
        document = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Retained date-only export",
            file=SimpleUploadedFile("dates.csv", data, "text/csv"),
        )
        CommunicationContext.objects.create(
            case=self.case,
            message=date_only,
            source_document=document,
            source_sha256=document.sha256,
            source_row=1,
            source_start=len(b"Sender,Date\n"),
            source_end=len(data),
            parser_version="imessage-csv-v1",
            timezone="UTC",
            raw_sender="sender-1",
            raw_timestamp="2026-04-01",
        )
        self.assertContains(self.client.get(self.writing_url()), "2 messages selected")
        response = self.post(first_start="2026-04-01", first_end="2026-04-01")
        self.assertEqual(response.status_code, 302)
        run = AnalysisRun.objects.get(operation="style_profiles")
        self.assertEqual(
            [source["evidence_id"] for source in run.inputs], [str(timed.pk)]
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
        self.assertEqual(run.parameters["writing_scope"]["period_split"], 1)
        self.assertEqual(
            [source["evidence_id"] for source in run.inputs],
            [str(earlier.pk), str(later.pk)],
        )

        bounded = self.post(first_end=self.periods["first_end"])
        self.assertEqual(bounded.status_code, 302)
        profile = AnalysisRun.objects.get(operation="style_profiles")
        self.assertEqual(
            [source["evidence_id"] for source in profile.inputs], [str(earlier.pk)]
        )

    def test_invalid_or_empty_periods_do_not_queue(self):
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
        self.assertFalse(AnalysisRun.objects.exists())

    def test_publication_rechecks_evidence_revision_after_streaming_verification(self):
        self.login("owner")
        original = self.message()
        response = self.post()
        self.assertEqual(response.status_code, 302)
        run = AnalysisRun.objects.get(operation="style_profiles")

        def insert_after_verification(case, scope, snapshots):
            verify_membership(case, scope, snapshots)
            self.message(title="Later original")

        with patch(
            "core.analysis.worker.verify_membership",
            side_effect=insert_after_verification,
        ):
            self.assertEqual(process_run(run.pk), "writing_evidence_changed")
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        self.assertFalse(hasattr(run, "result"))
        self.assertContains(self.client.get(response.url), "Case evidence changed")

        retry, created, issue = retry_analysis(
            self.users["owner"], self.case.pk, run.pk
        )
        self.assertTrue(created, issue)
        self.assertEqual(retry.inputs, run.inputs)
        self.assertEqual(retry.inputs[0]["evidence_id"], str(original.pk))
        process_run(retry.pk)
        retry.refresh_from_db()
        self.assertEqual(retry.status, "complete", retry.error_code)
        self.assertEqual(retry.result.data["metrics"]["sources_selected"], 1)

    def test_full_uneven_periods_are_retained_and_retried_with_exact_coverage(self):
        self.login("owner")
        self.client.cookies["pandect_timezone"] = "UTC"
        amounts = (419, 788)
        rows = []
        for day, amount in zip((1, 3), amounts, strict=True):
            for index in range(amount):
                unique = index if day == 1 else index + amounts[0]
                suffix = "".join(
                    chr(97 + unique // place % 26) for place in (676, 26, 1)
                )
                body = f"The route marker{suffix} was present at waypoint{suffix}."
                if day == 3 and index >= amount - 2:
                    body = f"{body} QUANTUM LATCH SIGNAL!"
                row = TextMessageEvidence(
                    case=self.case,
                    uploaded_by=self.users["owner"],
                    sender=self.person,
                    recipient=self.peer,
                    title=f"Synthetic source {day}-{index}",
                    occurred_at=datetime(2026, 4, day, tzinfo=UTC)
                    + timedelta(minutes=index),
                    body=body,
                )
                row._prepare_original()
                rows.append(row)
        TextMessageEvidence.objects.bulk_create(rows)
        self.assertContains(
            self.client.get(self.writing_url()), "1207 messages selected"
        )
        counts = self.client.get(
            self.writing_url(compare=True),
            self.periods | {"mode": "compare", "writing_counts": 1},
        )
        self.assertContains(counts, "419 earlier · 788 later")
        submission_id = str(uuid4())
        selection = self.periods | {"submission_id": submission_id}
        response = self.post(compare=True, **selection)
        self.assertEqual(response.status_code, 302)
        original = AnalysisRun.objects.get(operation="style_change")
        self.assertEqual(
            [source["evidence_id"] for source in original.inputs],
            [str(row.pk) for row in rows],
        )
        self.assertEqual(
            original.parameters["writing_scope"]["period_split"], amounts[0]
        )
        self.assertContains(self.client.get(response.url), "1207 selected messages")

        changed = rows[217]

        def archive_after_processing(*arguments):
            result = analyze_attributed_writing(*arguments)
            TextMessageEvidence.objects.filter(pk=changed.pk).update(
                is_archived=True,
                archive_changed_at=timezone.now(),
                archive_changed_by=self.users["owner"],
            )
            return result

        with patch(
            "text.analysis.attributed_writing.analyze_attributed_writing",
            side_effect=archive_after_processing,
        ):
            self.assertEqual(process_run(original.pk), "source_archived")
        original.refresh_from_db()
        self.assertEqual(original.status, "failed")
        self.assertFalse(hasattr(original, "result"))
        TextMessageEvidence.objects.filter(pk=changed.pk).update(is_archived=False)
        additional = self.message(occurred_at=datetime(2026, 4, 3, 15, tzinfo=UTC))
        self.assertEqual(self.post(compare=True, **selection).url, response.url)

        retry, created, issue = retry_analysis(
            self.users["owner"], self.case.pk, original.pk
        )
        self.assertTrue(created, issue)
        self.assertEqual(retry.inputs, original.inputs)
        self.assertNotIn(
            str(additional.pk), {source["evidence_id"] for source in retry.inputs}
        )
        process_run(retry.pk)
        retry.refresh_from_db()
        self.assertEqual(retry.status, "complete", retry.error_code)
        self.assertEqual(process_run(retry.pk), "complete")
        self.assertEqual(retry.result.provenance["source_inputs"], original.inputs)
        data = retry.result.data
        self.assertEqual(data["metrics"]["sources_selected"], sum(amounts))
        measurements = next(
            item["values"]
            for item in data["items"]
            if item["label"] == "Vocabulary ratio"
        )
        for label, group in zip(
            ("group_a", "group_b"),
            (rows[: amounts[0]], rows[amounts[0] :]),
            strict=True,
        ):
            words = [
                word.lower()
                for row in group
                for word in re.findall(r"[A-Za-z]+", row.body)
            ]
            self.assertEqual(
                measurements[label], round(len(set(words)) / len(words), 4)
            )
        signal = next(
            item for item in data["items"] if item["text"] == "quantum latch signal"
        )
        self.assertEqual(signal["values"]["group_b_occurrences"], 2)
        self.assertEqual(
            {anchor["source_id"] for anchor in signal["anchors"]},
            {source["id"] for source in retry.inputs[-2:]},
        )
        result_url = reverse("core:analysis:detail", args=[self.case.pk, retry.pk])
        self.assertContains(self.client.get(result_url), "1207 messages examined")
        self.assertContains(
            self.client.get(result_url, {"source_page": 25}), rows[-1].title
        )
        download_url = reverse("core:analysis:download", args=[self.case.pk, retry.pk])
        self.assertContains(self.client.get(download_url), str(rows[-1].pk))
        evidence_url = reverse(
            "core:evidence:detail", args=[self.case.pk, "text-message", changed.pk]
        )
        self.assertContains(self.client.get(evidence_url), result_url)

        TextMessageEvidence.objects.filter(pk=changed.pk).update(body="Changed text")
        self.assertContains(self.client.get(result_url), "Result withheld")
        self.assertEqual(self.client.get(download_url).status_code, 404)
        TextMessageEvidence.objects.filter(pk=changed.pk).update(body=changed.body)

        self.assertEqual(self.post().status_code, 302)
        profile = AnalysisRun.objects.get(operation="style_profiles")
        self.assertEqual(len(profile.inputs), sum(amounts) + 1)
        process_run(profile.pk)
        profile.refresh_from_db()
        self.assertEqual(profile.status, "complete", profile.error_code)
        self.assertEqual(
            profile.result.data["metrics"]["sources_usable"], sum(amounts) + 1
        )

    def test_foreign_person_and_unrelated_user_cannot_access_route(self):
        self.login("owner")
        CaseMembership.objects.create(case=self.other_case, user=self.users["owner"])
        self.assertEqual(
            self.client.get(self.writing_url(person=self.foreign)).status_code, 404
        )
        self.login("outsider")
        self.assertEqual(self.client.get(self.writing_url()).status_code, 404)
        self.assertEqual(self.post().status_code, 404)
