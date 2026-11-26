"""Records beyond former UI cutoffs remain reachable within current case access."""

from unittest.mock import patch
from uuid import uuid4

from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import QueryDict
from django.test import TestCase
from django.urls import reverse

from cases.models import CaseMembership
from core.analysis.form_base import source_choices
from core.analysis.presentation import measurement_formatter
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from geo.models import VehicleLocationEvidence
from phone.models import CommunicationContext, PhoneCallEvidence, TextMessageEvidence
from text.models import TextEvidence


class CollectionPaginationTests(EvidenceViewTestMixin, TestCase):
    def test_report_page_links_apply_default_filters_and_recheck_case_access(self):
        PhoneCallEvidence.objects.bulk_create(
            [
                PhoneCallEvidence(
                    case=self.case,
                    uploaded_by=self.users["owner"],
                    sender=self.person,
                    recipient=self.person,
                    title=f"Call {number}",
                    sha256="0" * 64,
                )
                for number in range(30)
            ]
        )
        self.login("viewer")
        url = reverse("core:analysis:report", args=(self.case.pk, "calls"))
        second = self.client.get(url, {"page": 2})
        self.assertFalse(second.context["form"].errors)
        self.assertEqual(second.context["data"]["page_obj"].number, 2)
        self.assertEqual(second.context["data"]["metrics"]["Selected records"], 30)
        self.assertEqual(len(second.context["data"]["items"]), 5)
        self.assertEqual(
            self.client.get(url, {"pair_page": 2}).context["data"]["metrics"][
                "Selected records"
            ],
            30,
        )
        CaseMembership.objects.filter(
            case=self.case, user=self.users["viewer"]
        ).delete()
        self.assertEqual(self.client.get(url, {"page": 2}).status_code, 404)

    def test_thread_and_reply_pages_include_records_beyond_100_and_20(self):
        source = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic export",
            file=SimpleUploadedFile("source.html", b"Synthetic rows"),
        )
        messages = []
        for row in range(1, 106):
            message = TextMessageEvidence.objects.create(
                case=self.case,
                uploaded_by=self.users["owner"],
                sender=self.person,
                recipient=self.person,
                title=f"Row {row}",
                body=f"Synthetic {row}",
            )
            CommunicationContext.objects.create(
                case=self.case,
                message=message,
                source_document=source,
                source_sha256=source.sha256,
                source_row=row,
                source_start=row,
                source_end=row + 1,
                parser_version="test",
                timezone="UTC",
                raw_sender="Synthetic account",
                native_message_id="same-id",
                native_thread_id="thread",
                reply_to_id="same-id",
            )
            messages.append(message)
        self.login("viewer")
        url = self.url("detail", messages[-1], kind="text-message")
        first = self.client.get(url)
        self.assertEqual(first.context["native_thread_messages"].paginator.count, 105)
        self.assertEqual(len(first.context["native_thread_messages"]), 25)
        last = self.client.get(url, {"thread_page": 5, "reply_page": 5, "page": 2})
        self.assertEqual(
            [row.source_row for row in last.context["native_thread_messages"]],
            list(range(101, 106)),
        )
        self.assertEqual(
            [row.source_row for row in last.context["native_reply_candidates"]],
            list(range(101, 106)),
        )
        self.assertTrue(last.context["info_open"])
        self.assertContains(last, "thread_page=4")
        self.assertContains(last, "reply_page=4")
        CaseMembership.objects.filter(
            case=self.case, user=self.users["viewer"]
        ).delete()
        self.assertEqual(self.client.get(url, {"thread_page": 5}).status_code, 404)

    def test_source_pages_preserve_selection_and_search_without_starting_analysis(self):
        records = TextEvidence.objects.bulk_create(
            [
                TextEvidence(
                    case=self.case,
                    uploaded_by=self.users["owner"],
                    title=f"Synthetic source {number:03}",
                    body="Synthetic body",
                    sha256="0" * 64,
                )
                for number in range(503)
            ]
        )
        choices = source_choices(self.case, "entities")
        for number in range(2, 22):
            choices = source_choices(
                self.case, "entities", page=number, cursor=choices.after
            )
        self.assertEqual(len(choices), 3)
        self.assertFalse(choices.has_next())
        self.login("owner")
        url = (
            reverse("core:analysis:create", args=(self.case.pk,))
            + "?operation=entities"
        )
        first = self.client.get(url)
        selected = first.context["form"].source_page[0][0]
        post = {
            "operation": "entities",
            "submission_id": str(uuid4()),
            "sources": [selected],
            "sources_page": "1",
            "browse_sources": "next",
            "source_after": first.context["form"].source_page.after,
        }
        with patch("core.analysis.submission_views.start_selected_analysis") as start:
            last = self.client.post(url, post)
            start.assert_not_called()
        self.assertEqual(last.context["form"].source_page.number, 2)
        self.assertEqual(last.context["form"].retained_sources[0][0], selected)
        self.assertContains(last, f'type="hidden" name="sources" value="{selected}"')
        added = last.context["form"].source_page[-1][0]
        from core.case_search.discovery import Discovery

        def lookup(case, actor, query):
            results = Discovery()
            if query == records[250].title:
                results.evidence["text"].add(str(records[250].pk))
            return results

        self.enterContext(
            patch("core.case_search.discovery.discover", side_effect=lookup)
        )
        with patch("core.analysis.submission_views.start_selected_analysis") as start:
            searched = self.client.post(
                url,
                post
                | {
                    "sources": [selected, added],
                    "browse_sources": "search",
                    "q": records[250].title,
                },
            )
            start.assert_not_called()
        self.assertEqual(len(searched.context["form"].source_page), 1)
        self.assertEqual(len(searched.context["form"].retained_sources), 2)
        with patch("core.analysis.submission_views.start_selected_analysis") as start:
            empty = self.client.post(
                url,
                post
                | {
                    "sources": [selected, added],
                    "browse_sources": "search",
                    "q": "No matching source",
                },
            )
            self.assertContains(empty, "No suitable sources")
            self.assertNotContains(empty, 'href="?operation=entities">Clear search')
            cleared = self.client.post(
                url,
                post
                | {
                    "sources": [selected, added],
                    "browse_sources": "clear",
                    "q": "No matching source",
                },
            )
            start.assert_not_called()
        self.assertEqual(
            set(cleared.context["form"].data.getlist("sources")), {selected, added}
        )
        with patch(
            "core.analysis.submission_views.start_selected_analysis",
            side_effect=ValueError("should not queue"),
        ) as start:
            removed = self.client.post(url, post | {"remove_source": selected})
            start.assert_not_called()
        self.assertEqual(removed.context["form"].retained_sources, [])

    def test_vehicle_observations_and_checksum_matches_have_complete_pages(self):
        VehicleLocationEvidence.objects.bulk_create(
            [
                VehicleLocationEvidence(
                    case=self.case,
                    uploaded_by=self.users["owner"],
                    vehicle=self.vehicle,
                    title=f"Location {number}",
                    address="Synthetic address",
                    sha256="0" * 64,
                )
                for number in range(30)
            ]
        )
        self.login("viewer")
        detail = self.client.get(
            reverse("vehicle:detail", args=(self.case.pk, self.vehicle.pk)),
            {"locations_page": 2},
        )
        self.assertEqual(detail.context["locations"].paginator.count, 30)
        self.assertEqual(len(detail.context["locations"]), 5)
        TextEvidence.objects.bulk_create(
            [
                TextEvidence(
                    case=self.case,
                    uploaded_by=self.users["owner"],
                    title=f"Match {number}",
                    body="Synthetic body",
                    sha256="a" * 64,
                )
                for number in range(30)
            ]
        )
        self.text()
        matches = self.client.get(self.url("list"), {"checksum": "a" * 64, "page": 2})
        self.assertEqual(matches.context["page_obj"].paginator.count, 30)
        self.assertEqual(len(matches.context["rows"]), 5)
        self.assertContains(matches, "checksum=" + "a" * 64)

    def test_structured_lists_and_mappings_have_independent_value_pages(self):
        data = {
            "examples": [f"Value {number}" for number in range(205)],
            "mapping": {f"Key {number}": number for number in range(205)},
        }
        formatter = measurement_formatter(data, case=self.case)
        rows = formatter(data)
        self.assertNotIn("Value 204", rows[0]["text"])
        next_url = rows[0]["pagination"]["next"]
        query = QueryDict(next_url.split("?", 1)[1].split("#", 1)[0])
        second = measurement_formatter(data, case=self.case, query=query)(data)
        self.assertIn("Value 204", second[0]["text"])
        self.assertEqual(second[0]["pagination"]["page"].number, 2)
        mapping_page = rows[1]["children"][-1]["pagination"]["next"]
        query = QueryDict(mapping_page.split("?", 1)[1].split("#", 1)[0])
        second = measurement_formatter(data, case=self.case, query=query)(data)
        self.assertEqual(second[1]["children"][4]["text"], "204")
        self.assertEqual(second[0]["pagination"]["page"].number, 1)
