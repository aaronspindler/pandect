"""Call history CSV review through the normal case evidence entry points."""

import csv
import io
from uuid import uuid4

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from core.tests.testing import EvidenceViewTestMixin
from people.models import Person
from phone.call_history import HEADERS
from phone.call_history_jobs import process_call_history
from phone.models import CallHistoryImport, CallPersonLink, PhoneCallEvidence

NUMBER = "+17055550100"


def call_history_csv(*rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=HEADERS)
    writer.writeheader()
    for values in rows or ({},):
        writer.writerow(
            {
                "Call type": "Incoming",
                "Date": "2025-02-10 15:30:00",
                "Duration": "00:00:25",
                "Number": NUMBER,
                "Contact": "Synthetic caller",
                "Location": "",
                "Service": "Phone",
            }
            | values
        )
    return stream.getvalue().encode("utf-8")


class CallHistoryViewTests(EvidenceViewTestMixin, TestCase):
    def upload_url(self):
        return reverse("phone:call_history_upload", args=[self.case.pk])

    def review_url(self, batch, query=""):
        return (
            reverse("phone:call_history_review", args=[self.case.pk, batch.pk]) + query
        )

    def upload(self, content):
        data = {
            "submission_id": str(uuid4()),
            "file": SimpleUploadedFile("synthetic-calls.csv", content, "text/csv"),
        }
        response = self.client.post(self.upload_url(), data)
        self.assertEqual(response.status_code, 302)
        return response.url

    def prepare(self, content, **choices):
        source_url = self.upload(content)
        data = {
            "submission_id": str(uuid4()),
            "source_label": "Synthetic line",
        }
        response = self.client.post(source_url, data | choices)
        self.assertEqual(response.status_code, 302)
        batch = CallHistoryImport.objects.latest("created_at")
        self.assertRedirects(
            response, self.review_url(batch), fetch_redirect_response=False
        )
        self.assertEqual(
            process_call_history.func(str(batch.pk), str(batch.attempt_id)), "review"
        )
        batch.refresh_from_db()
        return batch

    def choose(self, batch, query="", **choices):
        response = self.client.post(
            self.review_url(batch, query),
            {
                "action": "save",
                "reviewed_attempt": str(batch.attempt_id),
            }
            | choices,
        )
        self.assertEqual(response.status_code, 302)
        batch.refresh_from_db()

    def publish(self, batch, query="", **choices):
        self.choose(batch, query, action="publish", **choices)
        self.assertEqual(
            process_call_history.func(str(batch.pk), str(batch.attempt_id)), "completed"
        )
        batch.refresh_from_db()

    def test_unmatched_calls_publish_and_owner_can_add_an_attributed_link(self):
        self.login("owner")
        self.assertContains(self.client.get(self.url("create")), self.upload_url())
        batch = self.prepare(
            call_history_csv(
                {"Number": ""},
                {"Date": "2025-11-02 01:30:00", "Contact": "Held for ambiguous clock"},
            )
        )
        review = self.client.get(self.review_url(batch, "?view=held"))
        self.assertContains(review, "Held for ambiguous clock")
        self.assertContains(review, "This date occurs twice in the export timezone.")
        self.assertContains(review, "Publish calls")
        self.assertNotContains(review, "Save choices")

        self.login("viewer")
        self.assertEqual(self.client.get(self.upload_url()).status_code, 403)
        response = self.client.get(self.review_url(batch))
        self.assertContains(response, "An owner or editor can choose matches")
        self.assertNotContains(response, "Publish calls")
        self.assertEqual(self.client.post(self.review_url(batch)).status_code, 403)
        self.login("outsider")
        self.assertEqual(self.client.get(self.review_url(batch)).status_code, 404)

        self.login("owner")
        self.publish(batch)
        self.assertContains(
            self.client.get(self.review_url(batch)),
            "1 published · 0 skipped · 1 held for review",
        )
        call = PhoneCallEvidence.objects.get()
        self.assertIsNone(call.sender_id)
        self.assertIsNone(call.recipient_id)
        link = reverse(
            "phone:link_imported_person", args=[self.case.pk, "phone-call", call.pk]
        )
        self.assertContains(self.client.get(link), "Link a person to the call")
        previous_sha256 = call.sha256
        outsider = Person.objects.create(
            case=self.other_case, first_name="Other person"
        )
        self.assertEqual(
            self.client.post(
                link, {"role": "sender", "person": outsider.pk}
            ).status_code,
            400,
        )
        response = self.client.post(link, {"role": "sender", "person": self.person.pk})
        self.assertRedirects(
            response,
            self.url("detail", call, "phone-call"),
            fetch_redirect_response=False,
        )
        call.refresh_from_db()
        self.assertEqual(call.sender_id, self.person.pk)
        self.assertEqual(call.sha256, previous_sha256)
        self.assertEqual(
            CallPersonLink.objects.get(row=call.import_row).linked_by,
            self.users["owner"],
        )

    def test_group_choices_and_overlaps_allow_individual_exceptions(self):
        self.login("editor")
        prior = self.prepare(call_history_csv())
        self.publish(prior)
        batch = self.prepare(
            call_history_csv({}, {"Date": "2025-02-10 15:31:00"}),
            source_label="",
            source=prior.source_id,
        )
        grouped = self.client.get(self.review_url(batch))
        self.assertContains(grouped, "?view=rows&amp;group=2")
        self.assertNotContains(grouped, "identifier=")
        self.assertContains(grouped, "Review possible overlaps before publishing")
        self.assertNotContains(grouped, ">Publish calls</button>")
        attempted = self.client.post(
            self.review_url(batch),
            {"action": "publish", "reviewed_attempt": str(batch.attempt_id)},
        )
        self.assertContains(
            attempted, "Choose how to handle possible overlaps", status_code=400
        )
        overlap_url = self.review_url(batch, "?view=overlaps")
        overlaps = self.client.get(overlap_url)
        self.assertContains(
            overlaps, '<option value="" selected>Choose how to handle overlaps</option>'
        )
        self.assertContains(
            overlaps, "1 matching row in this export and 1 in earlier exports."
        )
        self.assertContains(overlaps, f"{self.review_url(prior)}?view=rows")

        self.choose(batch, group_0=self.person.pk)
        self.choose(batch, "?view=rows&group=2", person_2="")
        self.publish(batch, "?view=overlaps", overlap_keep="skip", overlap_2="keep")
        calls = PhoneCallEvidence.objects.filter(import_row__batch=batch).order_by(
            "import_row__number"
        )
        self.assertEqual(
            list(calls.values_list("sender_id", flat=True)), [None, self.person.pk]
        )

    def test_retained_document_route_is_scoped_and_can_continue_the_review(self):
        self.login("owner")
        batch = self.prepare(call_history_csv())
        source_url = self.url("detail", batch.document, "document")
        source = self.client.get(source_url)
        self.assertContains(source, self.review_url(batch))
        self.assertContains(source, f"{self.upload_url()}?document={batch.document_id}")
        setup_url = f"{self.upload_url()}?document={batch.document_id}"
        setup = self.client.get(setup_url)
        self.assertContains(setup, "Review call history source")
        self.assertContains(setup, batch.document.original_filename)
        self.assertNotContains(setup, 'type="file"')
        processing = self.client.get(
            reverse("core:analysis:processing", args=[self.case.pk])
        )
        self.assertContains(processing, "Call history imports to review")
        self.assertContains(processing, self.review_url(batch))
        self.login("outsider")
        self.assertEqual(self.client.get(setup_url).status_code, 404)

    def test_new_upload_has_only_csv_and_uses_account_timezone_by_default(self):
        owner = self.users["owner"]
        owner.timezone = "America/Vancouver"
        owner.save(update_fields=("timezone",))
        self.login("owner")
        upload = self.client.get(self.upload_url())
        self.assertContains(upload, 'type="file"', count=1)
        for field in (
            "document",
            "timezone",
            "confirm_timezone",
            "source",
            "source_label",
        ):
            self.assertNotContains(upload, f'name="{field}"')

        source_url = self.upload(call_history_csv())
        self.assertFalse(CallHistoryImport.objects.exists())
        self.assertContains(self.client.get(source_url), "America/Vancouver")
        response = self.client.post(
            source_url, {"submission_id": str(uuid4()), "source_label": "Work phone"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(CallHistoryImport.objects.get().timezone, "America/Vancouver")

    def test_source_review_can_confirm_a_different_export_timezone(self):
        self.login("owner")
        batch = self.prepare(call_history_csv(), timezone="America/Halifax")
        self.assertEqual(batch.timezone, "America/Halifax")

    def test_header_only_export_shows_review_without_a_publish_action(self):
        self.login("owner")
        batch = self.prepare((",".join(HEADERS) + "\n").encode())
        review = self.client.get(self.review_url(batch, "?view=overlaps"))
        self.assertContains(review, "No calls are ready to publish.")
        self.assertNotContains(review, "Publish calls")
