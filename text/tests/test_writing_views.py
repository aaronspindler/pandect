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
from core.models import AnalysisResult, AnalysisRun
from core.tests.testing import EvidenceViewTestMixin
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

        process_run(run.pk)
        self.client.cookies["pandect_timezone"] = "America/Los_Angeles"
        details = self.client.get(response.url)
        self.assertContains(details, "Selected periods")
        self.assertContains(
            details, "2026-04-01 to 2026-04-01 and 2026-04-03 to 2026-04-03"
        )
        self.assertContains(details, "(America/Toronto)")

        self.client.cookies["pandect_timezone"] = "America/Toronto"
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

    def test_unrelated_change_during_verification_does_not_block_publication(self):
        self.login("owner")
        original = self.message()
        self.assertEqual(self.post().status_code, 302)
        run = AnalysisRun.objects.get(operation="style_profiles")
        verified = []

        def insert_after_verification(case, scope, snapshots):
            verify_membership(case, scope, snapshots)
            verified.append(snapshots)
            if len(verified) == 1:
                self.message(title="Later original")

        with patch(
            "core.analysis.worker.verify_membership",
            side_effect=insert_after_verification,
        ):
            process_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(verified, [run.inputs, run.inputs])
        self.assertEqual(run.status, "complete", run.error_code)
        self.assertEqual(run.inputs[0]["evidence_id"], str(original.pk))
        self.assertEqual(run.result.data["metrics"]["sources_selected"], 1)

    def test_selected_archive_during_verification_still_stops_publication(self):
        self.login("owner")
        original = self.message()
        self.assertEqual(self.post().status_code, 302)
        run = AnalysisRun.objects.get(operation="style_profiles")

        def archive_after_verification(case, scope, snapshots):
            verify_membership(case, scope, snapshots)
            TextMessageEvidence.objects.filter(pk=original.pk).update(
                is_archived=True,
                archive_changed_by=self.users["owner"],
                archive_changed_at=timezone.now(),
            )

        with patch(
            "core.analysis.worker.verify_membership",
            side_effect=archive_after_verification,
        ):
            self.assertEqual(process_run(run.pk), "source_archived")
        self.assertFalse(AnalysisResult.objects.filter(run=run).exists())

    def test_result_missing_at_first_lookup_waits_for_a_new_request(self):
        self.login("owner")
        self.message()
        self.assertEqual(self.post().status_code, 302)
        run = AnalysisRun.objects.get(operation="style_profiles")
        process_run(run.pk)
        detail = reverse("core:analysis:detail", args=[self.case.pk, run.pk])

        # Publication committed after this request took its first result snapshot.
        with (
            patch("core.analysis.run_views.case_run", return_value=run),
            patch("core.analysis.run_views.AnalysisResult") as results,
        ):
            results.objects.filter.return_value.first.return_value = None
            response = self.client.get(detail)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("data", response.context)
        self.assertIn("data", self.client.get(detail).context)

    def test_earlier_saved_exclusions_restore_links_without_rewriting_result(self):
        self.login("owner")
        self.client.cookies["pandect_timezone"] = "UTC"
        earlier = datetime(2026, 4, 1, 12, tzinfo=UTC)
        self.message(occurred_at=earlier)
        excluded = self.message(occurred_at=earlier, body='"This was quoted."')
        self.message(occurred_at=datetime(2026, 4, 3, 12, tzinfo=UTC))
        response = self.post(compare=True, **self.periods)
        self.assertEqual(response.status_code, 302)
        run = AnalysisRun.objects.get(operation="style_change")
        process_run(run.pk)
        data = run.result.data
        coverage = next(
            item for item in data["items"] if item["label"] == "Group A coverage"
        )
        example = coverage["values"]["excluded_span_examples"][0]
        source_key = example["source_id"]
        coverage["source_ids"].remove(source_key)
        coverage["anchors"] = [
            anchor
            for anchor in coverage["anchors"]
            if anchor["source_id"] != source_key
        ]
        AnalysisResult.objects.filter(pk=run.result.pk).update(data=data)
        page = self.client.get(response.url)
        item = next(
            item
            for item in page.context["items"]
            if item["label"] == "Group A coverage"
        )
        self.assertEqual(
            [link["anchors"] for link in item["sources"] if link["id"] == source_key],
            [[{"source_id": source_key, "start": 0, "end": len(excluded.body)}]],
        )
        run.result.refresh_from_db()
        self.assertEqual(run.result.data, data)

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
        download_url = reverse("core:analysis:download", args=[self.case.pk, retry.pk])
        with patch(
            "text.analysis.attributed_sources.verify_membership",
            side_effect=AssertionError("Warm reads must not scan all message bodies."),
        ):
            self.assertContains(
                self.client.get(result_url, {"source_page": 25}), rows[-1].title
            )
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
