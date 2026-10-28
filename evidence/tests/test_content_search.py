"""Retained content search respects source identity and the case boundary."""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone

from cases.models import CaseMembership
from evidence.models import CallEvidenceAttachment, DocumentEvidence, TextEvidence
from evidence.search import EXCERPT_LENGTH
from evidence.tests.test_views import EvidenceViewTestMixin
from evidence.types import KINDS
from people.models import Person


class RetainedContentSearchTests(EvidenceViewTestMixin, TestCase):
    def communication(self, kind, **values):
        return KINDS[kind].objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title=f"Synthetic {kind}",
            sender=self.person,
            recipient=self.person,
            **values,
        )

    def addition(self, call, transcript):
        parent = "phone_call" if call.communication_kind == "phone-call" else "facetime"
        return CallEvidenceAttachment.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            transcript=transcript,
            **{parent: call},
        )

    def test_every_retained_text_source_is_searchable_for_all_case_roles(self):
        text = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Interview excerpt",
            body="A retained uncommon phrase from the source.",
        )
        message = self.communication(
            "text-message", body="The retained uncommon phrase is in this message."
        )
        calls = [self.communication(kind) for kind in ("phone-call", "facetime")]
        for call in calls:
            self.addition(call, "An earlier transcript without the requested words.")
            self.addition(call, "A retained uncommon phrase in a later addition.")
        expected = {text.pk, message.pk, *(call.pk for call in calls)}
        for role in ("owner", "editor", "viewer"):
            with self.subTest(role=role):
                self.login(role)
                response = self.client.get(
                    self.url("list"), {"q": "RETAINED UNCOMMON PHRASE"}
                )
                self.assertEqual(
                    {row["id"] for row in response.context["rows"]}, expected
                )
                self.assertEqual(response.context["page_obj"].paginator.count, 4)
                self.assertIn("private", response["Cache-Control"])
                self.assertIn("no-store", response["Cache-Control"])

    def test_matching_call_additions_return_one_call_and_link_to_exact_addition(self):
        self.login("viewer")
        for kind in ("phone-call", "facetime"):
            with self.subTest(kind=kind):
                call = self.communication(kind)
                self.addition(call, "No matching content here.")
                match = self.addition(call, "The collection phrase is here.")
                self.addition(call, "The collection phrase appears again.")
                response = self.client.get(
                    self.url("list"), {"q": "collection phrase", "type": kind}
                )
                rows = response.context["rows"]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["id"], call.pk)
                self.assertTrue(rows[0]["url"].endswith(f"#attachment-{match.pk}"))
                self.assertIn("type=" + kind, rows[0]["url"])
                self.assertContains(response, "First matching supplied transcript")
                detail = self.client.get(rows[0]["url"])
                self.assertContains(detail, f'id="attachment-{match.pk}"')

    def test_content_excerpts_escape_markup_and_are_bounded_around_the_match(self):
        self.login("owner")
        text = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Escaping example",
            body="x" * 2000 + 'needle <script>alert("source")</script> ' + "z" * 2000,
        )
        response = self.client.get(self.url("list"), {"q": "needle"})
        row = response.context["rows"][0]
        self.assertEqual(row["id"], text.pk)
        self.assertIn("needle", row["content_match"]["excerpt"])
        self.assertLessEqual(len(row["content_match"]["excerpt"]), EXCERPT_LENGTH)
        self.assertContains(
            response, "&lt;script&gt;alert(&quot;source&quot;)&lt;/script&gt;"
        )
        self.assertNotContains(response, '<script>alert("source")</script>')

    def test_archive_type_filters_and_pagination_still_apply(self):
        self.login("owner")
        archived = self.communication("text-message", body="Pagination needle")
        archived.is_archived = True
        archived.archive_changed_at = timezone.now()
        archived.archive_changed_by = self.users["owner"]
        archived.save(
            update_fields=["is_archived", "archive_changed_at", "archive_changed_by"]
        )
        for number in range(26):
            TextEvidence.objects.create(
                case=self.case,
                uploaded_by=self.users["owner"],
                title=f"Page source {number}",
                body="Pagination needle",
            )
        active = self.client.get(self.url("list"), {"q": "Pagination needle"})
        self.assertEqual(active.context["page_obj"].paginator.count, 26)
        self.assertEqual(len(active.context["rows"]), 25)
        second = self.client.get(
            self.url("list") + active.context["pagination"]["next"]
        )
        self.assertEqual(len(second.context["rows"]), 1)
        self.assertNotIn(
            second.context["rows"][0]["id"],
            {row["id"] for row in active.context["rows"]},
        )
        response = self.client.get(
            self.url("list"),
            {"q": "Pagination needle", "state": "archived", "type": "text-message"},
        )
        self.assertEqual([row["id"] for row in response.context["rows"]], [archived.pk])
        all_records = self.client.get(
            self.url("list"), {"q": "Pagination needle", "state": "all"}
        )
        self.assertEqual(all_records.context["page_obj"].paginator.count, 27)

    def test_literal_wildcards_and_unknown_uploaded_contents(self):
        self.login("owner")
        expected = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Literal punctuation",
            body="A literal %_ sequence in source text.",
        )
        TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Unrelated text",
            body="This must not match wildcard syntax.",
        )
        DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Unparsed document",
            file=SimpleUploadedFile("unparsed.txt", b"A literal %_ sequence."),
        )
        response = self.client.get(self.url("list"), {"q": "%_"})
        self.assertEqual([row["id"] for row in response.context["rows"]], [expected.pk])

    def test_foreign_sources_and_revoked_access_cannot_expose_content(self):
        self.login("viewer")
        TextEvidence.objects.create(
            case=self.other_case,
            uploaded_by=self.users["outsider"],
            title="Hidden source",
            body="Cross-boundary needle",
        )
        call = self.communication("phone-call")
        addition = self.addition(call, "Cross-boundary needle")
        # Trusted-maintenance corruption must not bypass reader relation checks.
        CallEvidenceAttachment.objects.filter(pk=addition.pk).update(
            case=self.other_case
        )
        message = self.communication("text-message", body="Cross-boundary needle")
        other_person = Person.objects.create(
            case=self.other_case, first_name="Outsider"
        )
        KINDS["text-message"].objects.filter(pk=message.pk).update(sender=other_person)
        response = self.client.get(self.url("list"), {"q": "Cross-boundary needle"})
        self.assertEqual(response.context["page_obj"].paginator.count, 0)
        foreign = self.client.get(
            self.url("list", case=self.other_case), {"q": "Cross-boundary needle"}
        )
        self.assertEqual(foreign.status_code, 404)
        self.assertNotContains(foreign, "Hidden source", status_code=404)
        CaseMembership.objects.filter(
            case=self.case, user=self.users["viewer"]
        ).delete()
        revoked = self.client.get(self.url("list"), {"q": "Cross-boundary needle"})
        self.assertEqual(revoked.status_code, 404)

    def test_call_audio_filename_search_remains_available_without_a_transcript(self):
        self.login("viewer")
        call = self.communication("phone-call")
        CallEvidenceAttachment.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            phone_call=call,
            file=SimpleUploadedFile("harbour-recording.wav", b"synthetic audio"),
        )
        response = self.client.get(self.url("list"), {"q": "harbour-recording"})
        self.assertEqual([row["id"] for row in response.context["rows"]], [call.pk])
        self.assertIsNone(response.context["rows"][0]["content_match"])
