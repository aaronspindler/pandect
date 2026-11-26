from uuid import uuid4

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from cases.models import CaseMembership
from core.analysis.services import start_run
from core.analysis.worker import process_run
from core.testing import EvidenceViewTestMixin
from geo.models import PersonLocationEvidence
from people.models import Person
from phone.models import (
    CallEvidenceAttachment,
    FaceTimeEvidence,
    PhoneCallEvidence,
    TextMessageEvidence,
)
from text.models import TextEvidence


class RelatedEvidenceTests(EvidenceViewTestMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.peer = Person.objects.create(case=cls.case, first_name="Peer")
        cls.foreign_person = Person.objects.create(
            case=cls.other_case, first_name="Foreign person"
        )

    def communication(self, model=TextMessageEvidence, **changes):
        values = {
            "case": self.case,
            "uploaded_by": self.users["owner"],
            "title": "Related communication",
            "sender": self.person,
            "recipient": self.peer,
        }
        if model is TextMessageEvidence:
            values["body"] = "Synthetic message"
        return model.objects.create(**(values | changes))

    def person_url(self):
        return reverse("people:detail", args=[self.case.pk, self.person.pk])

    def test_explicit_associations_and_self_message_have_distinct_roles(self):
        sent = self.communication(title="Outgoing")
        received = self.communication(
            PhoneCallEvidence,
            title="Incoming call",
            sender=self.peer,
            recipient=self.person,
        )
        self_message = self.communication(
            FaceTimeEvidence, title="Self call", recipient=self.person
        )
        location = PersonLocationEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Reported location",
            person=self.person,
            address="Synthetic location",
        )
        self.communication(sender=self.peer, recipient=self.peer, title="Unrelated")
        TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic person mentioned",
            body="Synthetic person is mentioned without an explicit relationship.",
        )
        for role in ("owner", "editor", "viewer"):
            self.login(role)
            response = self.client.get(self.url("list"), {"person": self.person.pk})
            rows = response.context["rows"]
            self.assertEqual(
                {row["id"]: row["association"] for row in rows},
                {
                    sent.pk: "Sender",
                    received.pk: "Recipient",
                    self_message.pk: "Both",
                    location.pk: "Location",
                },
            )
            self.assertEqual(len(rows), 4)
            self.assertNotContains(response, "Unrelated")
            self.assertNotContains(response, "Synthetic person mentioned")

    def test_foreign_invalid_and_corrupt_relationships_are_excluded(self):
        self.login("owner")
        for value in (self.foreign_person.pk, uuid4(), "invalid"):
            self.assertEqual(
                self.client.get(self.url("list"), {"person": value}).status_code,
                404,
            )
        corrupted = self.communication(title="Corrupted relationship")
        TextMessageEvidence.objects.filter(pk=corrupted.pk).update(
            recipient=self.foreign_person
        )
        response = self.client.get(self.url("list"), {"person": self.person.pk})
        self.assertEqual(response.context["page_obj"].paginator.count, 0)
        for role in ("viewer", "outsider"):
            self.login(role)
            if role == "viewer":
                CaseMembership.objects.filter(
                    case=self.case, user=self.users[role]
                ).delete()
            self.assertEqual(self.client.get(self.person_url()).status_code, 404)
            self.assertEqual(
                self.client.get(
                    self.url("list"), {"person": self.person.pk}
                ).status_code,
                404,
            )

    def test_person_detail_excludes_archived_and_paginates_independently(self):
        for index in range(27):
            self.communication(title=f"Communication {index:02}")
        archived = TextMessageEvidence.objects.order_by("created_at").first()
        TextMessageEvidence.objects.filter(pk=archived.pk).update(
            is_archived=True,
            archive_changed_at=timezone.now(),
            archive_changed_by=self.users["owner"],
        )
        self.login("viewer")
        response = self.client.get(
            self.person_url(), {"evidence_page": 2, "poi_page": 3, "page": 4}
        )
        self.assertEqual(response.status_code, 200)
        related = response.context["related_evidence"]
        self.assertEqual(related["page_obj"].paginator.count, 26)
        self.assertEqual(related["page_obj"].number, 2)
        self.assertEqual(len(related["rows"]), 1)
        self.assertNotEqual(related["rows"][0]["id"], archived.pk)
        self.assertContains(response, "poi_page=3")
        self.assertContains(response, "page=4")
        self.assertContains(response, "Related findings")
        self.assertIn(f"person={self.person.pk}", related["rows"][0]["url"])

    def test_reader_and_search_preserve_person_filter(self):
        selected = self.communication(title="Needle")
        self.communication(sender=self.peer, recipient=self.peer, title="Needle other")
        self.login("viewer")
        params = {
            "person": str(self.person.pk),
            "q": "Needle",
            "state": "all",
            "type": "text-message",
        }
        response = self.client.get(self.url("list"), params)
        self.assertEqual(response.context["page_obj"].paginator.count, 1)
        reader = self.client.get(response.context["rows"][0]["url"])
        self.assertEqual(reader.context["item"], selected)
        self.assertEqual(reader.context["selected_person"], self.person)
        self.assertIn(f"person={self.person.pk}", reader.context["list_url"])
        self.assertContains(reader, 'name="person"')

    def test_unrelated_type_filter_returns_empty_collection(self):
        self.login("owner")
        response = self.client.get(
            self.url("list"), {"person": self.person.pk, "type": "document"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["page_obj"].paginator.count, 0)

    def test_related_findings_require_the_finding_to_cite_related_evidence(self):
        message = self.communication(body="linked@example.test")
        unrelated = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Unrelated text",
            body="unrelated@example.test",
        )
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "entities",
            [
                {"kind": "text-message", "id": str(message.pk)},
                {"kind": "text", "id": str(unrelated.pk)},
            ],
        )
        process_run(run.pk)
        self.login("viewer")
        response = self.client.get(self.person_url())
        self.assertContains(response, "linked@example.test")
        self.assertNotContains(response, "unrelated@example.test")
        self.assertEqual(
            response.context["related_findings"]["page_obj"].paginator.count, 1
        )

    def test_call_addition_findings_keep_their_precise_source(self):
        call = self.communication(PhoneCallEvidence)
        additions = [
            CallEvidenceAttachment.objects.create(
                case=self.case,
                uploaded_by=self.users["owner"],
                phone_call=call,
                transcript=f"{name}@example.test",
            )
            for name in ("first", "second")
        ]
        for attachment in additions:
            run, _ = start_run(
                self.users["owner"],
                self.case.pk,
                "entities",
                [
                    {
                        "kind": "phone-call",
                        "id": str(call.pk),
                        "attachment_id": str(attachment.pk),
                    }
                ],
            )
            process_run(run.pk)
        self.login("viewer")
        response = self.client.get(self.person_url())
        self.assertEqual(
            response.context["related_evidence"]["page_obj"].paginator.count, 1
        )
        self.assertEqual(
            response.context["related_findings"]["page_obj"].paginator.count, 2
        )
        self.assertContains(response, "first@example.test")
        self.assertContains(response, "second@example.test")
