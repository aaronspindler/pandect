"""Conversation reading, exact evidence links, and current case boundaries."""

import re
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

from django.core.files.base import ContentFile
from django.core.paginator import Paginator
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from cases.models import CaseMembership
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from people.models import Person
from phone.models import CommunicationContext, TextMessageEvidence


class ConversationViewTests(EvidenceViewTestMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.correspondent = Person.objects.create(case=cls.case, first_name="Blair")
        cls.third_person = Person.objects.create(case=cls.case, first_name="Casey")
        cls.foreign_person = Person.objects.create(
            case=cls.other_case, first_name="Foreign person"
        )

    def message(self, **values):
        return TextMessageEvidence.objects.create(
            **(
                {
                    "case": self.case,
                    "uploaded_by": self.users["owner"],
                    "sender": self.person,
                    "recipient": self.correspondent,
                    "title": "Synthetic message",
                    "body": "Preserved original café\r\n  spacing.  ",
                }
                | values
            )
        )

    def document(self, **values):
        return DocumentEvidence.objects.create(
            **(
                {
                    "case": self.case,
                    "uploaded_by": self.users["owner"],
                    "title": "Retained export",
                    "file": ContentFile(b"Synthetic export", name="messages.csv"),
                }
                | values
            )
        )

    def context(self, message, document, row=1, **values):
        return CommunicationContext.objects.create(
            **(
                {
                    "case": message.case,
                    "message": message,
                    "source_document": document,
                    "source_sha256": document.sha256,
                    "source_row": row,
                    "source_start": row - 1,
                    "source_end": row,
                    "parser_version": "synthetic-v1",
                    "timezone": "America/Toronto",
                    "raw_sender": "Source account",
                    "native_message_id": f"source-message-{row}",
                    "source_context": {"raw_fields": {"Status": "Read"}},
                }
                | values
            )
        )

    def reader_url(self, first=None, second=None):
        first, second = sorted(
            [(first or self.person).pk, (second or self.correspondent).pk]
        )
        return reverse(
            "phone:conversation_detail",
            kwargs={
                "case_id": self.case.pk,
                "first_person_id": first,
                "second_person_id": second,
            },
        )

    def list_url(self):
        return reverse("phone:conversation_list", kwargs={"case_id": self.case.pk})

    def test_current_members_can_read_and_revocation_and_superuser_do_not_bypass(self):
        message = self.message()
        for role in ("owner", "editor", "viewer"):
            self.login(role)
            self.assertContains(self.client.get(self.list_url()), "Blair")
            self.assertContains(self.client.get(self.reader_url()), message.body)
            self.assertEqual(self.client.post(self.reader_url()).status_code, 405)
        self.login("viewer")
        CaseMembership.objects.filter(
            case=self.case, user=self.users["viewer"]
        ).delete()
        self.assertEqual(self.client.get(self.reader_url()).status_code, 404)
        outsider = self.users["outsider"]
        outsider.is_superuser = True
        outsider.save(update_fields=["is_superuser"])
        self.login("outsider")
        for url in (self.list_url(), self.reader_url()):
            self.assertEqual(self.client.get(url).status_code, 404)

    def test_unordered_pairs_self_messages_archives_and_source_overlap_remain_visible(
        self,
    ):
        instant = datetime(2026, 11, 10, 12, tzinfo=UTC)
        first = self.message(occurred_at=instant)
        second = self.message(
            sender=self.correspondent,
            recipient=self.person,
            occurred_at=instant + timedelta(minutes=1),
            is_archived=True,
            archive_changed_at=timezone.now(),
            archive_changed_by=self.users["owner"],
        )
        self.context(first, self.document(title="First export"))
        self.context(second, self.document(title="Overlapping export"))
        own = self.message(recipient=self.person, title="Self message")
        self.login("viewer")
        response = self.client.get(self.list_url())
        pairs = list(response.context["page_obj"])
        self.assertEqual(sorted(row["message_count"] for row in pairs), [1, 2])
        response = self.client.get(self.reader_url())
        self.assertEqual(
            [row.pk for row in response.context["page_obj"]], [first.pk, second.pk]
        )
        self.assertContains(response, "Archived")
        self.assertContains(response, "First export")
        self.assertContains(response, "Overlapping export")
        self.assertNotContains(response, own.title)
        self.assertIsNone(response.context["selected_message"])

    def test_message_details_are_preloaded_and_open_only_for_explicit_deep_link(self):
        export = self.document()
        first = self.message(title="First retained message")
        second = self.message(title="Second retained message")
        self.context(
            first,
            export,
            row=1,
            native_thread_id="shared-thread",
            reply_to_id="source-message-2",
        )
        self.context(second, export, row=2, native_thread_id="shared-thread")
        self.login("viewer")
        response = self.client.get(self.reader_url())
        self.assertIsNone(response.context["selected_message"])
        for message in (first, second):
            tag = self.details_tag(response, message)
            self.assertNotRegex(tag, r"\bopen(?:\s|=|>)")
            self.assertContains(
                response, self.url("detail", message, kind="text-message")
            )
        self.assertContains(response, "source-message-1")
        self.assertContains(response, "source-message-2")
        self.assertContains(response, "shared-thread")
        self.assertNotContains(response, 'id="native-thread"')
        self.assertNotContains(response, 'id="native-replies"')
        self.assertNotContains(response, "Unresolved: no matching native ID")
        self.assertContains(response, "Read", count=2)
        linked = self.client.get(self.reader_url(), {"message": second.pk})
        self.assertRegex(self.details_tag(linked, second), r"\bopen(?:\s|=|>)")
        self.assertNotRegex(self.details_tag(linked, first), r"\bopen(?:\s|=|>)")
        evidence = self.client.get(self.url("detail", first, kind="text-message"))
        self.assertContains(evidence, 'id="native-thread"')
        self.assertContains(evidence, 'id="native-replies"')
        self.assertEqual(
            [
                context.message_id
                for context in evidence.context["native_reply_candidates"]
            ],
            [second.pk],
        )
        self.assertEqual(
            {
                context.message_id
                for context in evidence.context["native_thread_messages"]
            },
            {first.pk, second.pk},
        )

    def details_tag(self, response, message):
        match = re.search(
            rf'<details\b[^>]*\bid="message-details-{message.pk}"[^>]*>',
            response.content.decode(),
        )
        self.assertIsNotNone(match, "Each message needs native inline details")
        return match.group()

    def test_time_order_ties_unknowns_and_deep_links_beyond_first_page(self):
        instant = datetime(2026, 11, 10, 12, tzinfo=UTC)
        messages = [
            self.message(
                id=UUID(int=1000 + number),
                occurred_at=instant + timedelta(minutes=number // 2),
                title=f"Timed message {number}",
            )
            for number in range(52)
        ]
        earlier_unknown = self.message(id=UUID(int=1500), title="Earlier unknown")
        unknown = self.message(id=UUID(int=2000), title="Time unknown")
        self.login("viewer")
        response = self.client.get(self.reader_url())
        self.assertEqual(
            [row.pk for row in response.context["page_obj"]],
            [row.pk for row in messages[:50]],
        )
        response = self.client.get(
            self.reader_url(), {"message": messages[51].pk, "page": 1}
        )
        self.assertEqual(response.context["page_obj"].number, 2)
        self.assertEqual(response.context["selected_message"].pk, messages[51].pk)
        self.assertEqual(
            [row.pk for row in response.context["page_obj"]],
            [messages[50].pk, messages[51].pk, earlier_unknown.pk, unknown.pk],
        )
        self.assertContains(response, f'id="message-{messages[51].pk}"')
        self.assertContains(response, "Time not recorded")
        response = self.client.get(self.reader_url(), {"message": unknown.pk})
        self.assertEqual(response.context["selected_message"].pk, unknown.pk)
        response = self.client.get(self.reader_url(), {"page": 2})
        self.assertIsNone(response.context["selected_message"])

    def test_import_between_rank_and_page_keeps_requested_message_selected(self):
        instant = datetime(2026, 11, 10, 12, tzinfo=UTC)
        messages = [
            self.message(
                occurred_at=instant + timedelta(minutes=number),
                title=f"Retained message {number}",
            )
            for number in range(50)
        ]
        target = messages[-1]
        original_get_page = Paginator.get_page
        inserted = False

        def import_before_page_read(paginator, number):
            nonlocal inserted
            if not inserted:
                inserted = True
                self.message(
                    occurred_at=instant - timedelta(minutes=1),
                    title="Earlier message published during navigation",
                )
            return original_get_page(paginator, number)

        self.login("viewer")
        with patch(
            "phone.conversation_views.Paginator.get_page",
            autospec=True,
            side_effect=import_before_page_read,
        ):
            response = self.client.get(self.reader_url(), {"message": target.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["page_obj"].number, 2)
        self.assertEqual(response.context["selected_message"].pk, target.pk)
        self.assertEqual([row.pk for row in response.context["page_obj"]], [target.pk])
        self.assertContains(response, f'id="message-{target.pk}"')
        self.assertIn(str(target.pk), response.context["selected_message"].evidence_url)

    def test_selected_message_removed_before_page_read_returns_not_found(self):
        instant = datetime(2026, 11, 10, 12, tzinfo=UTC)
        self.message(occurred_at=instant, title="Earlier retained message")
        target = self.message(
            occurred_at=instant + timedelta(minutes=1), title="Requested message"
        )
        self.message(
            occurred_at=instant + timedelta(minutes=2), title="Later retained message"
        )
        original_get_page = Paginator.get_page
        removed = False

        def remove_before_page_read(paginator, number):
            nonlocal removed
            if not removed:
                removed = True
                TextMessageEvidence.objects.filter(pk=target.pk).delete()
            return original_get_page(paginator, number)

        self.login("viewer")
        with patch(
            "phone.conversation_views.Paginator.get_page",
            autospec=True,
            side_effect=remove_before_page_read,
        ):
            response = self.client.get(self.reader_url(), {"message": target.pk})
        self.assertEqual(response.status_code, 404)
        self.assertNotContains(response, "Open evidence", status_code=404)

    def test_export_filter_excludes_other_sources_and_manual_messages(self):
        export = self.document()
        selected = self.message(title="From selected export")
        self.context(selected, export, reply_to_id="unresolved-reference")
        other = self.message(title="From another export")
        self.context(other, self.document(title="Other export"))
        self.message(title="Entered individually")
        self.login("viewer")
        response = self.client.get(self.reader_url(), {"source": export.pk})
        self.assertEqual(
            [row.pk for row in response.context["page_obj"]], [selected.pk]
        )
        self.assertEqual(response.context["selected_source"].pk, export.pk)
        self.assertContains(response, "unresolved-reference")
        self.assertContains(response, "Read")
        self.assertContains(response, f"/document/{export.pk}/")
        self.assertEqual(
            self.client.get(
                self.reader_url(), {"source": export.pk, "message": other.pk}
            ).status_code,
            404,
        )
        empty = self.document(title="No messages from this source")
        response = self.client.get(self.reader_url(), {"source": empty.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["page_obj"]), [])
        self.assertIsNone(response.context["selected_message"])

    def test_message_evidence_roundtrip_preserves_source_and_exact_target(self):
        export = self.document()
        messages = []
        for number in range(52):
            message = self.message(
                occurred_at=datetime(2026, 11, 10, 12, tzinfo=UTC)
                + timedelta(minutes=number)
            )
            self.context(message, export, row=number + 1)
            messages.append(message)
        self.login("viewer")
        response = self.client.get(
            self.reader_url(),
            {"source": export.pk, "message": messages[-1].pk, "list_page": 2},
        )
        evidence_url = response.context["selected_message"].evidence_url
        self.assertEqual(
            parse_qs(urlsplit(evidence_url).query)["conversation_source"],
            [str(export.pk)],
        )
        self.assertEqual(
            parse_qs(urlsplit(evidence_url).query)["conversation_list_page"], ["2"]
        )
        evidence = self.client.get(evidence_url)
        self.assertContains(evidence, "View in conversation")
        conversation_url = evidence.context["conversation_url"]
        params = parse_qs(urlsplit(conversation_url).query)
        self.assertEqual(params["source"], [str(export.pk)])
        self.assertEqual(params["message"], [str(messages[-1].pk)])
        self.assertEqual(params["list_page"], ["2"])
        self.assertEqual(
            urlsplit(conversation_url).fragment, f"message-{messages[-1].pk}"
        )
        returned = self.client.get(conversation_url)
        self.assertEqual(returned.context["page_obj"].number, 2)
        self.assertEqual(returned.context["selected_message"].pk, messages[-1].pk)
        self.assertEqual(
            returned.context["conversation_list_url"], self.list_url() + "?page=2"
        )

    def test_reversed_pair_redirect_preserves_reader_and_list_context(self):
        message = self.message()
        export = self.document()
        self.context(message, export)
        first, second = sorted((self.person.pk, self.correspondent.pk))
        reversed_url = reverse(
            "phone:conversation_detail",
            kwargs={
                "case_id": self.case.pk,
                "first_person_id": second,
                "second_person_id": first,
            },
        )
        self.login("viewer")
        response = self.client.get(
            reversed_url,
            {"source": export.pk, "page": 2, "message": message.pk, "list_page": 3},
        )
        self.assertEqual(response.status_code, 302)
        location = urlsplit(response["Location"])
        self.assertEqual(location.path, self.reader_url())
        self.assertEqual(
            parse_qs(location.query),
            {
                "source": [str(export.pk)],
                "page": ["2"],
                "message": [str(message.pk)],
                "list_page": ["3"],
            },
        )
        followed = self.client.get(response["Location"])
        self.assertEqual(followed.context["selected_message"].pk, message.pk)
        self.assertEqual(followed.context["page_obj"].number, 1)
        self.assertEqual(followed.context["list_page"], 3)

    def test_cross_case_invalid_and_inconsistent_relations_are_not_exposed(self):
        good = self.message(title="Visible message")
        corrupt = self.message(title="Corrupt participant")
        TextMessageEvidence.objects.filter(pk=corrupt.pk).update(
            recipient=self.foreign_person
        )
        other_pair = self.message(recipient=self.third_person, title="Different pair")
        foreign_export = self.document(
            case=self.other_case, title="Foreign retained export"
        )
        export = self.document(title="Corrupt context source")
        context = self.context(good, export)
        CommunicationContext.objects.filter(pk=context.pk).update(
            source_document=foreign_export
        )
        self.login("viewer")
        response = self.client.get(self.list_url())
        self.assertEqual(
            sum(row["message_count"] for row in response.context["page_obj"]), 2
        )
        response = self.client.get(self.reader_url())
        self.assertEqual([row.pk for row in response.context["page_obj"]], [good.pk])
        self.assertNotContains(response, foreign_export.title)
        self.assertNotContains(response, export.title)
        self.assertIsNone(list(response.context["page_obj"])[0].imported_source)
        for params in (
            {"message": corrupt.pk},
            {"message": other_pair.pk},
            {"message": uuid4()},
            {"message": "invalid"},
            {"source": foreign_export.pk},
            {"source": "invalid"},
        ):
            self.assertEqual(
                self.client.get(self.reader_url(), params).status_code, 404
            )
        self.assertEqual(
            self.client.get(self.reader_url(second=self.foreign_person)).status_code,
            404,
        )

        evidence_url = self.url("detail", good, kind="text-message")
        for value in ("invalid", str(foreign_export.pk)):
            self.assertEqual(
                self.client.get(
                    evidence_url, {"conversation_source": value}
                ).status_code,
                404,
            )

    def test_escaped_original_body_is_complete_and_not_executable(self):
        body = (
            "  <script>alert('untrusted')</script>\n" + "a" * 2048 + "\n  preserved.  "
        )
        message = self.message(body=body)
        self.login("viewer")
        response = self.client.get(self.reader_url())
        self.assertContains(
            response, "&lt;script&gt;alert(&#x27;untrusted&#x27;)&lt;/script&gt;"
        )
        self.assertNotContains(response, "<script>alert('untrusted')</script>")
        self.assertContains(response, "a" * 2048)
        self.assertContains(response, f"/text-message/{message.pk}/")

    def test_empty_conversations_and_database_pagination_bound_rows_and_queries(self):
        self.login("viewer")
        response = self.client.get(self.list_url())
        self.assertEqual(list(response.context["page_obj"]), [])
        first = self.message()
        export = self.document()
        self.context(first, export)
        with CaptureQueriesContext(connection) as one:
            self.client.get(self.reader_url())
        for number in range(1, 54):
            self.context(self.message(), export, row=number + 1)
        with CaptureQueriesContext(connection) as many:
            response = self.client.get(self.reader_url())
        self.assertEqual(len(response.context["page_obj"]), 50)
        self.assertLessEqual(
            len(many), len(one) + 2, "Conversation queries grew per message"
        )
        for number in range(26):
            person = Person.objects.create(
                case=self.case, first_name=f"Person {number}"
            )
            self.message(recipient=person)
        response = self.client.get(self.list_url())
        self.assertEqual(len(response.context["page_obj"]), 25)
        self.assertEqual(response.context["page_obj"].paginator.count, 27)
        second_page = self.client.get(self.list_url(), {"page": 2})
        row = list(second_page.context["page_obj"])[0]
        self.assertEqual(parse_qs(urlsplit(row["url"]).query)["list_page"], ["2"])
        reader = self.client.get(row["url"])
        self.assertEqual(
            reader.context["conversation_list_url"], self.list_url() + "?page=2"
        )
        self.assertContains(reader, f'href="{self.list_url()}?page=2"')
