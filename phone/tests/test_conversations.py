"""Conversation reading, exact evidence links, and current case boundaries."""

from datetime import UTC, datetime, timedelta
from html import escape
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
from phone.conversations import conversation_url
from phone.message_csv import IMESSAGE_PARSER_VERSION, WHATSAPP_PARSER_VERSION
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

    def test_unordered_pairs_and_self_messages_exclude_archives(self):
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
        self.assertEqual(sorted(row["message_count"] for row in pairs), [1, 1])
        response = self.client.get(self.reader_url())
        self.assertEqual([row.pk for row in response.context["page_obj"]], [first.pk])
        self.assertContains(response, "First export")
        self.assertNotContains(response, "Overlapping export")
        self.assertNotContains(response, own.title)
        self.assertIsNone(response.context["selected_message"])
        self.assertEqual(
            self.client.get(self.reader_url(), {"message": second.pk}).status_code,
            404,
        )
        archived_url = self.url("detail", second, kind="text-message")
        self.assertNotContains(self.client.get(archived_url), "View in conversation")

    def test_message_inspector_preloads_details_and_deep_links_to_one_message(self):
        export = self.document()
        first = self.message(
            title="First retained message",
            occurred_at=datetime(2026, 11, 10, 12, tzinfo=UTC),
        )
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
            self.assertContains(response, f"message={message.pk}")
            self.assertContains(
                response, self.url("detail", message, kind="text-message")
            )
        linked = self.client.get(
            self.reader_url(),
            {"message": first.pk, "source": export.pk, "list_page": 2},
        )
        self.assertEqual(linked.context["selected_message"].pk, first.pk)
        self.assertContains(linked, "data-selected-message", count=1)
        self.assertContains(linked, "data-message-panel", count=1)
        self.assertNotContains(response, "data-selected-message")
        for text in (
            first.body,
            "Reported sender",
            "Reported recipient",
            "Sent at",
            "Open evidence",
            "Message source",
            "View source metadata",
            export.title,
            "Row 1",
        ):
            self.assertContains(linked, text)
        for native_detail in (
            "source-message-1",
            "source-message-2",
            "shared-thread",
            'id="native-thread"',
            'id="native-replies"',
            "<dd>Read</dd>",
            "Not supplied",
        ):
            self.assertNotContains(response, native_detail)
            self.assertNotContains(linked, native_detail)
        self.assertNotContains(linked, "<dd>Active</dd>", html=True)
        source_url = linked.context["selected_message"].source_info_url
        self.assertContains(linked, f'href="{escape(source_url)}"')
        parsed_source = urlsplit(source_url)
        self.assertEqual(
            parsed_source.path, self.url("detail", first, kind="text-message")
        )
        self.assertEqual(parsed_source.fragment, "evidence-details-heading")
        self.assertEqual(
            parse_qs(parsed_source.query),
            {
                "conversation_list_page": ["2"],
                "info": ["1"],
            },
        )
        evidence = self.client.get(source_url)
        self.assertTrue(evidence.context["info_open"])
        self.assertContains(evidence, 'id="native-thread"')
        self.assertContains(evidence, 'id="native-replies"')
        self.assertContains(evidence, "source-message-1")
        self.assertContains(evidence, "source-message-2")
        self.assertContains(evidence, "shared-thread")
        self.assertContains(evidence, "<dd>Read</dd>", html=True)
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

    def test_jump_to_date_uses_display_timezone_and_keeps_the_full_timeline(self):
        midnight = datetime(2026, 11, 11, 5, tzinfo=UTC)
        earlier = [
            self.message(occurred_at=midnight - timedelta(minutes=52 - number))
            for number in range(52)
        ]
        target = self.message(occurred_at=midnight)
        later = self.message(occurred_at=midnight + timedelta(days=1))
        unknown = self.message()
        self.login("viewer")
        self.client.cookies["pandect_timezone"] = "America/Toronto"
        result = self.client.get(self.reader_url(), {"day": "2026-11-11"})
        page = result.context["page_obj"]
        self.assertEqual(page.number, 2)
        self.assertEqual(page.paginator.count, 55)
        self.assertEqual(list(page), [*earlier[50:], target, later, unknown])
        self.assertIsNone(result.context["selected_message"])
        self.assertContains(
            result,
            'data-conversation-day="2026-11-11" id="conversation-day-result"',
        )
        self.assertContains(result, 'href="?page=1"')
        no_match = self.client.get(self.reader_url(), {"day": "2026-11-14", "page": 2})
        self.assertEqual(list(no_match.context["page_obj"]), list(page))
        self.assertContains(no_match, "No messages were recorded on that date.")
        invalid = self.client.get(self.reader_url(), {"day": "2026-02-30"})
        self.assertContains(invalid, "Enter a valid date.")

        self.login("outsider")
        self.assertEqual(
            self.client.get(self.reader_url(), {"day": "2026-11-11"}).status_code, 404
        )

    def test_search_matches_literal_bodies_in_the_retained_timeline(self):
        instant = datetime(2026, 11, 10, 12, tzinfo=UTC)
        query = "crate%_<b>"
        metadata_only = self.message(title=query, occurred_at=instant)
        wildcard_only = self.message(
            body="crateXY<b>", occurred_at=instant + timedelta(minutes=1)
        )
        first = self.message(
            id=UUID(int=1),
            body="Load CRATE%_<b> beside crate%_<b>.",
            occurred_at=instant + timedelta(minutes=2),
        )
        copy = self.message(
            id=UUID(int=2), body=first.body, occurred_at=first.occurred_at
        )
        self.context(first, self.document(title="First export"))
        self.context(copy, self.document(title="Second export"))
        self.message(
            body="Retained crate%_<b>.",
            is_archived=True,
            archive_changed_at=timezone.now(),
            archive_changed_by=self.users["owner"],
        )
        self.message(recipient=self.third_person, body=query)
        self.message(
            case=self.other_case,
            sender=self.foreign_person,
            recipient=self.foreign_person,
            uploaded_by=self.users["outsider"],
            body=query,
        )
        self.login("viewer")

        reader = self.reader_url()
        first_match = self.client.get(reader, {"q": f"  {query}  "})
        self.assertContains(first_match, "Match 1 of 1")
        self.assertContains(first_match, "<mark>CRATE%_&lt;b&gt;</mark>")
        self.assertNotContains(first_match, "<mark>CRATE%_<b></mark>")
        self.assertContains(first_match, "<mark>", count=1)
        self.assertIsNone(first_match.context["selected_message"])
        self.assertEqual(
            list(first_match.context["page_obj"]),
            [metadata_only, wildcard_only, first],
        )
        self.assertNotContains(first_match, "Retained crate")
        self.assertContains(first_match, '#conversation-search-result"')

        self.login("outsider")
        self.assertEqual(self.client.get(reader, {"q": query}).status_code, 404)

    def test_search_reports_no_matches_and_overlong_queries(self):
        original = self.message(body="Unrelated original")
        self.login("viewer")
        reader = self.reader_url()
        missing = self.client.get(reader, {"q": "missing phrase"})
        self.assertContains(missing, "No matching messages.")
        self.assertContains(missing, 'value="missing phrase"')
        self.assertEqual(list(missing.context["page_obj"]), [original])

        overlong = self.client.get(reader, {"q": "x" * 201})
        self.assertContains(overlong, "at most 200 characters")
        self.assertEqual(list(overlong.context["page_obj"]), [original])

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

    def test_old_export_links_open_all_sources_and_preserve_original_metadata(self):
        export = self.document()
        first = self.message(title="From selected export")
        self.context(first, export, reply_to_id="unresolved-reference")
        other = self.message(title="From another export")
        self.context(other, self.document(title="Other export"))
        manual = self.message(title="Entered individually")
        self.login("viewer")
        response = self.client.get(
            self.reader_url(), {"source": export.pk, "message": other.pk}
        )
        self.assertEqual(
            {row.pk for row in response.context["page_obj"]},
            {first.pk, other.pk, manual.pk},
        )
        self.assertEqual(response.context["selected_message"].pk, other.pk)
        self.assertNotContains(response, "Apply filter")
        self.assertNotContains(response, "unresolved-reference")
        evidence = self.client.get(
            self.url("detail", first, kind="text-message"), {"info": "1"}
        )
        self.assertContains(evidence, "Unresolved: no matching native ID")

    def test_exact_cross_export_copies_share_a_row_and_open_each_original(self):
        instant = datetime(2026, 11, 10, 12, tzinfo=UTC)
        for minute in range(50):
            self.message(occurred_at=instant - timedelta(minutes=minute + 1))
        left, right = (
            self.document(title="First export"),
            self.document(title="Second export"),
        )
        first = self.message(id=UUID(int=1), occurred_at=instant)
        other = self.message(
            id=UUID(int=2),
            occurred_at=instant,
        )
        self.context(first, left, row=3, native_message_id="copied-id")
        self.context(other, right, row=7, native_message_id="copied-id")
        self.login("viewer")
        reader = self.reader_url()
        page = self.client.get(reader, {"page": 2})
        self.assertEqual(page.context["page_obj"].paginator.count, 51)
        self.assertEqual([row.pk for row in page.context["page_obj"]], [first.pk])
        self.assertContains(page, "Show 2 retained copies")
        self.assertNotContains(page, "Second export · Row 7")
        self.assertContains(page, f"page=2&amp;copies={first.pk}#copies-{first.pk}")
        expanded = self.client.get(reader, {"page": 2, "copies": first.pk})
        self.assertContains(expanded, "Hide 2 retained copies")
        self.assertContains(expanded, "First export · Row 3")
        self.assertContains(expanded, "Second export · Row 7")
        selected = self.client.get(reader, {"message": other.pk})
        actual = selected.context["selected_message"]
        self.assertEqual((actual.pk, actual.timeline_id), (other.pk, first.pk))
        self.assertEqual(selected.context["page_obj"].number, 2)
        self.assertContains(selected, f'id="message-{other.pk}"')
        self.assertContains(selected, f'data-message-key="{first.pk}"')
        self.assertContains(selected, self.url("detail", other, kind="text-message"))
        self.assertContains(selected, "Row 7")
        TextMessageEvidence.objects.filter(pk=other.pk).update(
            is_archived=True,
            archive_changed_by=self.users["owner"],
            archive_changed_at=timezone.now(),
        )
        archived = self.client.get(reader, {"page": 2, "copies": first.pk})
        self.assertNotContains(archived, "Second export · Row 7")
        self.assertNotContains(archived, "retained copies")
        self.assertEqual(
            self.client.get(reader, {"message": other.pk}).status_code, 404
        )

    def test_long_copy_disclosure_paginates_each_source_record(self):
        instant = datetime(2026, 11, 10, 12, tzinfo=UTC)
        copies = []
        for number in range(26):
            message = self.message(id=UUID(int=number + 1), occurred_at=instant)
            self.context(
                message,
                self.document(title=f"Export {number:02}"),
                native_message_id="repeated-across-exports",
            )
            copies.append(message)
        self.login("viewer")
        first = self.client.get(self.reader_url())
        (row,) = first.context["page_obj"]
        self.assertEqual(row.copy_count, 26)
        self.assertNotContains(first, "Export 01")
        opened = self.client.get(self.reader_url(), {"copies": row.timeline_id})
        (open_row,) = opened.context["page_obj"]
        self.assertEqual(len(open_row.copy_page), 25)
        self.assertContains(opened, "Next copies")
        self.assertNotContains(opened, "Export 25")
        second = self.client.get(
            self.reader_url(), {"copies": row.timeline_id, "copy_page": 2}
        )
        (continued,) = second.context["page_obj"]
        self.assertEqual(
            [copy.message_id for copy in continued.copy_page], [copies[-1].pk]
        )
        self.assertContains(second, "Previous copies")
        self.assertContains(second, f"message={copies[-1].pk}")

    def test_ambiguous_or_unconfirmed_cross_export_records_stay_separate(self):
        instant = datetime(2026, 11, 10, 12, tzinfo=UTC)
        first_export, second_export = self.document(), self.document()
        records = [
            (first_export, "repeated", instant, "yes"),
            (first_export, "repeated", instant, "yes"),
            (second_export, "repeated", instant, "yes"),
            (first_export, "conflict", instant, "yes"),
            (second_export, "conflict", instant, "no"),
            (first_export, "", instant, "yes"),
            (second_export, "", instant, "yes"),
            (first_export, "missing time", None, "yes"),
            (second_export, "missing time", None, "yes"),
        ]
        for number, (export, native_id, time, body) in enumerate(records, 1):
            self.context(
                self.message(occurred_at=time, body=body),
                export,
                row=number,
                native_message_id=native_id,
            )
        for export, service in ((first_export, "SMS"), (second_export, "iMessage")):
            self.context(
                self.message(occurred_at=instant, body="identical"),
                export,
                row=100,
                native_message_id="shared-id",
                account_identifiers={"service": service},
            )
        self.login("viewer")
        page = self.client.get(self.reader_url()).context["page_obj"]
        self.assertEqual(len(page), len(records) + 2)
        self.assertTrue(all(row.copy_count == 1 for row in page))

    def test_whatsapp_native_ids_are_distinct_from_legacy_imessage_ids(self):
        instant = datetime(2026, 11, 10, 12, tzinfo=UTC)
        legacy = self.message(id=UUID(int=1), occurred_at=instant)
        self.context(
            legacy,
            self.document(title="iMessage export"),
            native_message_id="shared-id",
            parser_version=IMESSAGE_PARSER_VERSION,
        )
        first_export = self.document(title="First WhatsApp export")
        second_export = self.document(title="Second WhatsApp export")
        for number, export, native_id, source_row in (
            (2, first_export, "shared-id", 1),
            (3, first_export, "wa-id", 2),
            (4, second_export, "wa-id", 1),
        ):
            self.context(
                self.message(id=UUID(int=number), occurred_at=instant),
                export,
                row=source_row,
                native_message_id=native_id,
                parser_version=WHATSAPP_PARSER_VERSION,
                account_identifiers={"service": "WhatsApp"},
            )

        self.login("viewer")
        page = self.client.get(self.reader_url()).context["page_obj"]
        self.assertEqual(
            [(row.pk.int, row.copy_count) for row in page], [(1, 1), (2, 1), (3, 2)]
        )
        selected = self.client.get(self.reader_url(), {"message": UUID(int=2)})
        self.assertEqual(selected.context["selected_message"].timeline_id.int, 2)
        self.assertContains(selected, "First WhatsApp export")

    def test_message_evidence_roundtrip_preserves_list_and_exact_target(self):
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
        self.assertNotIn("conversation_source", parse_qs(urlsplit(evidence_url).query))
        self.assertEqual(
            parse_qs(urlsplit(evidence_url).query)["conversation_list_page"], ["2"]
        )
        evidence = self.client.get(evidence_url)
        self.assertContains(evidence, "View in conversation")
        selected_url = evidence.context["conversation_url"]
        params = parse_qs(urlsplit(selected_url).query)
        self.assertNotIn("source", params)
        self.assertEqual(params["message"], [str(messages[-1].pk)])
        self.assertEqual(params["list_page"], ["2"])
        self.assertEqual(urlsplit(selected_url).fragment, f"message-{messages[-1].pk}")
        returned = self.client.get(selected_url)
        self.assertEqual(returned.context["page_obj"].number, 2)
        self.assertEqual(returned.context["selected_message"].pk, messages[-1].pk)
        self.assertEqual(
            returned.context["conversation_list_url"], self.list_url() + "?page=2"
        )
        focused_url = conversation_url(self.case, messages[-1], open_details=False)
        focused = self.client.get(focused_url)
        self.assertEqual(focused.context["page_obj"].number, 2)
        self.assertIsNone(focused.context["selected_message"])
        self.assertContains(focused, f'id="{urlsplit(focused_url).fragment}"')
        self.assertEqual(
            self.client.get(self.reader_url(), {"focus": uuid4()}).status_code, 404
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
                200,
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
            response = self.client.get(self.reader_url())
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
