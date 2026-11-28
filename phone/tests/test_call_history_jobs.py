"""Synthetic call history parsing and worker publication boundaries."""

import csv
import io
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import Http404
from django.test import SimpleTestCase, TestCase

from cases.models import CaseMembership
from core.tests.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from people.models import Person
from phone.call_history import HEADERS, MAX_RECORD_CHARS, parse_call_history
from phone.call_history_jobs import (
    confirm_call_history,
    process_call_history,
    retain_call_history,
    retain_call_history_upload,
    retry_call_history,
    save_call_history_selection,
)
from phone.models import (
    CallHistoryImport,
    CallPersonLink,
    FaceTimeEvidence,
    PhoneCallEvidence,
)

NUMBER = "+12025550100"
BASE_ROW = {
    "Call type": "Incoming",
    "Date": "2026-10-07 10:20:30",
    "Duration": "00:00:00",
    "Number": NUMBER,
    "Contact": "Synthetic, contact",
    "Location": "",
    "Service": "Phone",
}


def csv_bytes(*rows, headers=HEADERS):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, headers)
    writer.writeheader()
    writer.writerows(BASE_ROW | row for row in rows)
    return output.getvalue().encode("utf-8")


class CallHistoryParserTests(SimpleTestCase):
    def test_raw_values_and_reordered_bom_headers(self):
        service = "FaceTime Video: 31d20d31-1441-4e4c-8f7d-22d2c84d1cc9"
        raw = csv_bytes(
            {
                "Call type": "Outgoing",
                "Number": "a@example.test",
                "Contact": " Synthetic,\r\n label  ",
                "Service": service,
            },
            headers=tuple(reversed(HEADERS)),
        )
        [row] = parse_call_history(b"\xef\xbb\xbf" + raw, "America/Toronto")
        self.assertEqual(row.raw["Contact"], " Synthetic,\r\n label  ")
        self.assertEqual(row.raw["Service"], service)
        self.assertEqual(row.kind, "facetime")
        self.assertEqual(row.direction, "outgoing")
        self.assertEqual(row.duration, timedelta())
        self.assertEqual(row.identifier, "a@example.test")
        self.assertEqual(row.occurred_at, datetime(2026, 10, 7, 14, 20, 30, tzinfo=UTC))
        self.assertEqual(len(row.fingerprint), 64)

    def test_ambiguous_nonexistent_and_unusable_identifiers_remain_distinct(self):
        rows = list(
            parse_call_history(
                csv_bytes(
                    {"Date": "2026-11-01 01:30:00"},
                    {"Date": "2026-03-08 02:30:00"},
                    {"Number": "", "Contact": "Unknown"},
                    {"Number": "Private", "Contact": ""},
                ),
                "America/Toronto",
            )
        )
        self.assertIn("occurs twice", rows[0].issue)
        self.assertIn("does not exist", rows[1].issue)
        self.assertEqual(rows[0].raw["Date"], "2026-11-01 01:30:00")
        for row in rows[2:]:
            self.assertEqual((row.issue, row.identifier, row.fingerprint), ("", "", ""))

    def test_bad_row_width_and_bounded_record_allocation(self):
        header = (",".join(HEADERS) + "\n").encode()
        [row] = parse_call_history(header + b"Incoming,too few\n", "UTC")
        self.assertEqual(row.number, 2)
        self.assertEqual(row.raw, {"Call type": "Incoming", "Date": "too few"})
        self.assertIn("seven columns", row.issue)
        too_many_values = b'"x\n",' * (MAX_RECORD_CHARS // 5 + 1) + b'"last"\n'
        with self.assertRaisesMessage(ValidationError, "oversized record"):
            list(parse_call_history(header + too_many_values, "UTC"))


class CallHistoryJobTests(EvidenceViewTestMixin, TestCase):
    def upload(self, content=None, *, source=None, actor="editor"):
        document = retain_call_history_upload(
            self.users[actor],
            self.case.pk,
            submission_id=uuid4(),
            original=SimpleUploadedFile(
                "synthetic.csv", content or csv_bytes({}), "text/csv"
            ),
        )
        return retain_call_history(
            self.users[actor],
            self.case.pk,
            submission_id=uuid4(),
            document=document,
            source=source,
            source_label="" if source else "Synthetic line",
            timezone="America/Toronto",
        )

    def work(self, batch):
        result = process_call_history.call(str(batch.pk), str(batch.attempt_id))
        batch.refresh_from_db()
        return result

    def choose(self, batch, **selection):
        result = save_call_history_selection(
            self.users["editor"],
            self.case.pk,
            batch.pk,
            reviewed_attempt=batch.attempt_id,
            **selection,
        )
        batch.refresh_from_db()
        return result

    def confirm(self, batch):
        result = confirm_call_history(
            self.users["editor"],
            self.case.pk,
            batch.pk,
            reviewed_attempt=batch.attempt_id,
        )
        batch.refresh_from_db()
        return result

    def test_first_step_retains_exact_upload_and_reuses_only_matching_retry(self):
        actor = self.users["editor"]
        submission = uuid4()
        content = csv_bytes({})

        def stage(value, *, name="history.csv"):
            return retain_call_history_upload(
                actor,
                self.case.pk,
                submission_id=submission,
                original=SimpleUploadedFile(name, value, "text/csv"),
            )

        document = stage(content)
        self.assertEqual(stage(content, name="retry.csv"), document)
        with document.file.open("rb") as source:
            self.assertEqual(source.read(), content)
        with self.assertRaisesMessage(ValidationError, "different material"):
            stage(csv_bytes({"Location": "Changed"}))
        self.assertEqual(DocumentEvidence.objects.count(), 1)
        self.assertFalse(CallHistoryImport.objects.exists())

    def test_upload_identity_is_distinct_per_submission_and_case(self):
        actor = self.users["editor"]
        submission = uuid4()

        def stage(case, identifier):
            return retain_call_history_upload(
                actor,
                case.pk,
                submission_id=identifier,
                original=SimpleUploadedFile("history.csv", csv_bytes({}), "text/csv"),
            )

        document = stage(self.case, submission)
        self.assertNotEqual(stage(self.case, uuid4()), document)
        with self.assertRaises(Http404):
            stage(self.other_case, submission)
        CaseMembership.objects.create(case=self.other_case, user=actor, role="editor")
        other_document = stage(self.other_case, submission)
        self.assertNotEqual(other_document, document)
        self.assertEqual(other_document.case, self.other_case)
        self.assertEqual(DocumentEvidence.objects.count(), 3)

    def test_import_publishes_mapped_and_unmatched_rows_and_holds_time_errors(self):
        content = csv_bytes(
            {},
            {"Service": "FaceTime Audio", "Call type": "Outgoing"},
            {"Number": "", "Contact": "Unknown"},
            {"Date": "2026-11-01 01:30:00"},
        )
        batch = self.upload(content)
        with batch.document.file.open("rb") as source:
            self.assertEqual(source.read(), content)
        self.assertEqual(self.work(batch), "review")
        self.assertFalse(PhoneCallEvidence.objects.exists())
        self.choose(batch, group_people={NUMBER: self.person.pk}, row_people={3: None})
        self.confirm(batch)
        published_attempt = batch.attempt_id
        self.assertEqual(self.work(batch), "completed")
        self.assertEqual(
            process_call_history.call(str(batch.pk), str(published_attempt)),
            "completed",
        )
        self.assertEqual(PhoneCallEvidence.objects.count(), 2)
        matched = PhoneCallEvidence.objects.get(import_row__number=2)
        unmatched = PhoneCallEvidence.objects.get(import_row__number=4)
        self.assertEqual(matched.sender, self.person)
        self.assertIsNone(matched.recipient)
        self.assertIsNone(unmatched.sender)
        self.assertIsNone(unmatched.recipient)
        facetime = FaceTimeEvidence.objects.get()
        self.assertIsNone(facetime.recipient)
        self.assertEqual(facetime.import_row.number, 3)
        self.assertEqual(facetime.original_data()["source"]["row"], 3)
        self.assertEqual(CallPersonLink.objects.get().person, self.person)
        held = batch.rows.get(number=5)
        self.assertEqual(held.disposition, "held")
        self.assertEqual(held.raw["Date"], "2026-11-01 01:30:00")

    def test_exact_export_reused_and_line_identity_controls_overlap(self):
        first = self.upload()
        again = self.upload(source=first.source)
        self.assertEqual(again.pk, first.pk)
        self.assertEqual(again.document_id, first.document_id)
        self.assertEqual(DocumentEvidence.objects.count(), 2)
        self.assertEqual(self.work(first), "review")
        self.confirm(first)
        self.assertEqual(self.work(first), "completed")
        other = retain_call_history(
            self.users["editor"],
            self.case.pk,
            submission_id=uuid4(),
            document=first.document,
            source=None,
            source_label="Other synthetic line",
            timezone="America/Toronto",
        )
        self.assertEqual(self.work(other), "review")
        self.assertEqual(other.rows.get().overlap_candidates, [])

    def test_changed_overlap_requires_fresh_review_and_a_row_can_be_kept(self):
        first = self.upload()
        second = self.upload(
            csv_bytes({"Contact": "Edited book name"}, {"Location": "View"}),
            source=first.source,
        )
        for batch in (first, second):
            self.assertEqual(self.work(batch), "review")
            self.confirm(batch)
        self.assertEqual(self.work(first), "completed")
        self.assertEqual(self.work(second), "review")
        self.assertEqual(second.error, "overlaps_changed")
        self.assertEqual(PhoneCallEvidence.objects.count(), 1)
        earlier = str(first.rows.get().pk)
        self.assertEqual(
            list(second.rows.values_list("overlap_candidates", flat=True)),
            [[earlier], [earlier]],
        )
        self.choose(second, overlap_keep=False, overlap_rows={2: True})
        self.confirm(second)
        self.assertEqual(self.work(second), "completed")
        self.assertEqual(
            list(second.rows.values_list("disposition", flat=True)),
            ["published", "skipped"],
        )
        self.assertEqual(PhoneCallEvidence.objects.count(), 2)
        self.assertEqual(DocumentEvidence.objects.count(), 2)

    def test_changed_candidates_discard_prior_skip_choices(self):
        first = self.upload()
        self.assertEqual(self.work(first), "review")
        self.confirm(first)
        self.assertEqual(self.work(first), "completed")
        second = self.upload(csv_bytes({"Location": "A"}), source=first.source)
        third = self.upload(csv_bytes({"Location": "B"}), source=first.source)
        self.assertEqual(self.work(second), "review")
        self.assertEqual(self.work(third), "review")
        self.choose(second, overlap_keep=True)
        self.choose(third, overlap_keep=False, overlap_rows={2: False})
        for batch in (second, third):
            self.confirm(batch)
        self.assertEqual(self.work(second), "completed")
        self.assertEqual(self.work(third), "review")
        self.assertEqual(third.error, "overlaps_changed")
        self.assertNotIn("overlap_keep", third.selection)
        self.assertNotIn("overlap_rows", third.selection)
        self.assertEqual(len(third.rows.get().overlap_candidates), 2)
        self.choose(third, group_people={NUMBER: self.person})
        with self.assertRaisesMessage(ValidationError, "Review possible overlaps"):
            self.confirm(third)
        self.choose(third, overlap_keep=True)
        self.confirm(third)
        self.assertEqual(self.work(third), "completed")
        self.assertEqual(third.rows.get().disposition, "published")

    def test_initial_overlap_needs_an_explicit_keep_or_skip_choice(self):
        first = self.upload()
        self.assertEqual(self.work(first), "review")
        self.confirm(first)
        self.assertEqual(self.work(first), "completed")
        for keep, disposition in ((True, "published"), (False, "skipped")):
            with self.subTest(keep=keep):
                batch = self.upload(
                    csv_bytes({"Location": disposition}), source=first.source
                )
                self.assertEqual(self.work(batch), "review")
                self.assertTrue(batch.rows.get().overlap_candidates)
                with self.assertRaisesMessage(
                    ValidationError, "Review possible overlaps"
                ):
                    self.confirm(batch)
                self.choose(batch, overlap_keep=keep)
                self.confirm(batch)
                self.assertEqual(self.work(batch), "completed")
                self.assertEqual(batch.rows.get().disposition, disposition)

    def test_current_scope_rechecked_before_background_work_and_for_choices(self):
        with self.assertRaises(PermissionDenied):
            self.upload(actor="viewer")
        with self.assertRaises(Http404):
            self.upload(actor="outsider")
        batch = self.upload()
        CaseMembership.objects.filter(
            case=self.case, user=self.users["editor"]
        ).delete()
        self.assertEqual(self.work(batch), "prepare_failed")
        self.assertEqual(batch.error, "access_removed")
        retry_call_history(self.users["owner"], self.case.pk, batch.pk)
        batch.refresh_from_db()
        self.assertEqual(self.work(batch), "review")
        other_person = Person.objects.create(
            case=self.other_case, first_name="Elsewhere"
        )
        with self.assertRaisesMessage(ValidationError, "Choose people in this case."):
            save_call_history_selection(
                self.users["owner"],
                self.case.pk,
                batch.pk,
                reviewed_attempt=batch.attempt_id,
                source_person=other_person,
            )

    def test_partial_failure_rolls_back_calls_and_retries_without_duplicate_work(self):
        batch = self.upload(csv_bytes({}, {"Service": "FaceTime Audio"}))
        self.assertEqual(self.work(batch), "review")
        self.choose(
            batch, source_person=self.person, group_people={NUMBER: self.person}
        )
        self.confirm(batch)
        with patch.object(
            FaceTimeEvidence.objects, "bulk_create", side_effect=RuntimeError
        ):
            self.assertEqual(self.work(batch), "publish_failed")
        self.assertFalse(PhoneCallEvidence.objects.exists())
        self.assertFalse(batch.rows.filter(disposition="published").exists())
        retry_call_history(self.users["editor"], self.case.pk, batch.pk)
        batch.refresh_from_db()
        with patch("core.analysis.automatic.start_call_history_analysis") as start:
            self.assertEqual(self.work(batch), "completed")
        start.assert_called_once_with(self.users["editor"], batch)
        self.assertEqual(PhoneCallEvidence.objects.count(), 1)
        self.assertEqual(FaceTimeEvidence.objects.count(), 1)
