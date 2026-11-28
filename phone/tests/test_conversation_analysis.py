"""Saved conversation findings stay contextual, bounded, and source-backed."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.core.files.base import ContentFile
from django.db import connection, transaction
from django.http import Http404
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from cases.models import CaseMembership
from core.analysis.catalogue import project_result
from core.analysis.operations import get_operation
from core.analysis.source_selection import snapshot_sources
from core.models import (
    AnalysisFindingSource,
    AnalysisResult,
    AnalysisReview,
    AnalysisRun,
)
from core.tests.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from people.models import Person
from phone.models import CommunicationContext, TextMessageEvidence


class ConversationAnalysisTests(EvidenceViewTestMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.correspondent = Person.objects.create(case=cls.case, first_name="Blair")
        cls.unrelated_person = Person.objects.create(case=cls.case, first_name="Casey")

    def setUp(self):
        super().setUp()
        self.first = self.message(title="First message")
        self.second = self.message(title="Second message")
        self.login("viewer")

    def message(self, **values):
        return TextMessageEvidence.objects.create(
            **(
                {
                    "case": self.case,
                    "uploaded_by": self.users["owner"],
                    "sender": self.person,
                    "recipient": self.correspondent,
                    "title": "Synthetic message",
                    "body": "An unchanged original message.",
                }
                | values
            )
        )

    def reader_url(self):
        first, second = sorted((self.person.pk, self.correspondent.pk))
        return reverse("phone:conversation_detail", args=(self.case.pk, first, second))

    def publish(
        self,
        *,
        messages=None,
        selections=None,
        items=None,
        operation="entities",
        parameters=None,
        status="complete",
        case=None,
    ):
        case = case or self.case
        inputs = snapshot_sources(
            case,
            selections
            if selections is not None
            else [
                {"kind": "text-message", "id": message.pk}
                for message in (messages if messages is not None else [self.first])
            ],
        )
        run = AnalysisRun.objects.create(
            case=case,
            requested_by=case.owner,
            operation=operation,
            method_version=get_operation(operation).version,
            parameters=parameters or {},
            inputs=inputs,
            status=status,
        )
        if callable(items):
            items = items(inputs)
        if items is None:
            items = [self.item(inputs, "A saved observation", 0)]
        result = AnalysisResult.objects.create(
            case=case, run=run, data={"items": items}
        )
        # Include legacy timing projections to exercise their reader opt-in.
        with patch("core.analysis.catalogue.is_timing_only", return_value=False):
            project_result(result)
        return result

    def item(self, inputs, text, *indices, label="Observation", values=None):
        return {
            "label": label,
            "text": text,
            "source_ids": [inputs[index]["id"] for index in indices],
            "values": values or {},
        }

    def analysis(self, response, message):
        return next(
            row.analysis for row in response.context["page_obj"] if row.pk == message.pk
        )

    def timing_item(self, inputs, *, shared_words=None, **values):
        return self.item(
            inputs,
            "Recorded temporal adjacency",
            0,
            1,
            label="Candidate exchange sequence",
            values={
                "before": {"source_id": inputs[0]["id"]},
                "after": {"source_id": inputs[1]["id"]},
                "elapsed_seconds": 16,
                "shared_words": shared_words or [],
                **values,
            },
        )

    def test_timing_only_findings_have_truthful_empty_state_and_explicit_reveal(self):
        result = self.publish(
            operation="conversation_link",
            messages=[self.first, self.second],
            items=lambda inputs: [self.timing_item(inputs)],
        )
        response = self.client.get(self.reader_url())
        self.assertEqual(response.context["conversation_analysis"]["count"], 0)
        self.assertEqual(response.context["conversation_timing_only_count"], 1)
        self.assertContains(response, "1 timing-only finding hidden")
        self.assertContains(response, "Show all findings")
        self.assertNotContains(response, "No saved findings")
        self.assertNotContains(response, "Recorded temporal adjacency")

        revealed = self.client.get(
            self.reader_url() + response.context["finding_visibility_url"]
        )
        shared = revealed.context["conversation_analysis"]
        self.assertEqual(shared["count"], 1)
        self.assertIsNotNone(shared["page_obj"])
        self.assertEqual(shared["rows"][0]["item_index"], 0)
        self.assertIn(str(result.run_id), shared["rows"][0]["url"])
        self.assertEqual(
            parse_qs(urlsplit(shared["rows"][0]["url"]).query)["weak"], ["1"]
        )
        self.assertContains(revealed, "Hide timing-only findings")

    def test_timing_filter_runs_before_counts_and_preserves_supported_candidates(self):
        self.publish(
            operation="conversation_link",
            messages=[self.first, self.second],
            items=lambda inputs: (
                [self.timing_item(inputs) for _ in range(28)]
                + [
                    self.timing_item(inputs, shared_words=["harbour"]),
                    self.timing_item(inputs, status="unresolved"),
                    self.timing_item(inputs, after={"reply_to_id": "native-reference"}),
                ]
            ),
        )
        response = self.client.get(self.reader_url())
        shared = response.context["conversation_analysis"]
        self.assertEqual(shared["count"], 3)
        self.assertEqual(len(shared["rows"]), 3)
        self.assertEqual(response.context["conversation_timing_only_count"], 28)
        self.assertEqual([row["item_index"] for row in shared["rows"]], [28, 29, 30])

    def test_legacy_acceptance_does_not_override_timing_filter(
        self,
    ):
        result = self.publish(
            operation="conversation_link",
            messages=[self.first, self.second],
            items=lambda inputs: [self.timing_item(inputs)],
        )
        for item_index in (None, 0):
            with self.subTest(item_index=item_index):
                AnalysisReview.objects.create(
                    case=self.case,
                    result=result,
                    reviewed_by=self.users["owner"],
                    item_index=item_index,
                    decision="accepted",
                    comment="A retained review reason.",
                )
                response = self.client.get(self.reader_url())
                self.assertEqual(response.context["conversation_analysis"]["count"], 0)
                self.assertEqual(response.context["conversation_timing_only_count"], 1)

    def test_reveal_links_preserve_message_context_and_reset_finding_pages(
        self,
    ):
        export = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic timing export",
            file=ContentFile(b"Synthetic rows", name="timing.csv"),
        )
        messages = [self.first, self.second] + [self.message() for _ in range(49)]
        for number, message in enumerate(messages, 1):
            CommunicationContext.objects.create(
                case=self.case,
                message=message,
                source_document=export,
                source_sha256=export.sha256,
                source_row=number,
                source_start=number - 1,
                source_end=number,
                parser_version="synthetic-v1",
                timezone="America/Toronto",
                raw_sender="Synthetic source account",
            )
        self.publish(
            operation="conversation_link",
            messages=[self.first, self.second],
            items=lambda inputs: [self.timing_item(inputs) for _ in range(26)],
        )
        params = {
            "source": str(export.pk),
            "message": str(self.first.pk),
            "list_page": "2",
            "weak": "1",
            "analysis_page": "2",
            "conversation_analysis_page": "2",
        }
        response = self.client.get(self.reader_url(), params)
        shared = response.context["conversation_analysis"]
        self.assertEqual(shared["count"], 26)
        self.assertEqual(len(shared["rows"]), 1)
        for url in (
            shared["previous_url"],
            shared["rows"][0]["message_sources"][0]["url"],
        ):
            query = parse_qs(urlsplit(url).query)
            for key in ("list_page", "weak"):
                self.assertEqual(query[key], [params[key]])
        self.assertEqual(parse_qs(response.context["page_query"])["weak"], ["1"])
        toggle = parse_qs(urlsplit(response.context["finding_visibility_url"]).query)
        self.assertNotIn("weak", toggle)
        self.assertNotIn("analysis_page", toggle)
        self.assertEqual(toggle["conversation_analysis_page"], ["1"])
        self.assertEqual(toggle["message"], [str(self.first.pk)])
        self.assertNotIn("source", toggle)

        first, second = sorted((self.person.pk, self.correspondent.pk))
        reversed_url = reverse(
            "phone:conversation_detail", args=(self.case.pk, second, first)
        )
        canonical = self.client.get(reversed_url, params)
        self.assertEqual(canonical.status_code, 302)
        self.assertEqual(parse_qs(urlsplit(canonical.url).query)["weak"], ["1"])

    def test_individual_citations_determine_relevance_and_inline_or_shared_placement(
        self,
    ):
        unrelated = self.message(
            sender=self.unrelated_person,
            recipient=self.unrelated_person,
            title="Unrelated exchange",
        )
        note = self.text()
        self.publish(
            selections=[
                {"kind": "text-message", "id": message.pk}
                for message in (self.first, self.second, unrelated)
            ]
            + [{"kind": "text", "id": note.pk}],
            items=lambda inputs: [
                self.item(inputs, "One message only", 0),
                self.item(inputs, "Unrelated finding must stay out", 2),
                self.item(inputs, "A comparison between these messages", 0, 1),
                self.item(inputs, "A message and another evidence type", 0, 3),
            ],
        )
        response = self.client.get(self.reader_url())
        inline = self.analysis(response, self.first)
        shared = response.context["conversation_analysis"]
        self.assertEqual(inline["count"], 1)
        self.assertEqual(inline["rows"][0]["text"], "One message only")
        self.assertEqual(self.analysis(response, self.second)["count"], 0)
        self.assertEqual(shared["count"], 2)
        self.assertEqual(
            {row["text"] for row in shared["rows"]},
            {
                "A comparison between these messages",
                "A message and another evidence type",
            },
        )
        outside = next(
            row for row in shared["rows"] if row["text"].endswith("evidence type")
        )
        self.assertTrue(outside["outside_sources"])
        self.assertNotContains(response, "Unrelated finding must stay out")

    def test_mixed_model_results_have_exact_ai_labels_and_sessions_stay_shared(self):
        self.publish(
            operation="text_topics",
            items=lambda inputs: [
                self.item(
                    inputs,
                    "Frequent observed word; a candidate content label.",
                    0,
                    label="harbour",
                    values={"occurrences": 2},
                ),
                self.item(
                    inputs,
                    "A suggested topic",
                    0,
                    label="Topic",
                    values={"status": "suggestion"},
                ),
            ],
        )
        self.publish(
            operation="sessions",
            items=lambda inputs: [self.item(inputs, "An inferred session", 0)],
        )
        self.publish(
            operation="content_events",
            items=lambda inputs: [
                self.item(
                    inputs, "Delivery was confirmed.", 0, values={"status": "reported"}
                )
            ],
        )
        self.publish(
            operation="session_summaries",
            messages=[self.first, self.second],
            items=lambda inputs: [
                self.item(
                    inputs,
                    "Summary of a larger selected exchange",
                    0,
                    values={
                        "status": "suggestion",
                        "chunk_source_ids": [source["id"] for source in inputs],
                    },
                )
            ],
        )
        self.publish(
            operation="comm_patterns",
            items=lambda inputs: [
                self.item(
                    inputs, "An unnecessary original echo", 0, label="Selected message"
                ),
                self.item(inputs, "A timing comparison", 0, label="Observed session 1"),
            ],
        )
        response = self.client.get(self.reader_url())
        inline = self.analysis(response, self.first)
        self.assertEqual(inline["count"], 1)
        self.assertEqual(
            {row["text"]: row["ai_suggestion"] for row in inline["rows"]},
            {"Delivery was confirmed.": True},
        )
        shared = response.context["conversation_analysis"]
        self.assertEqual(shared["count"], 1)
        self.assertEqual(shared["rows"][0]["text"], "An inferred session")
        self.assertNotContains(response, "A suggested topic")
        self.assertNotContains(response, "Summary of a larger selected exchange")
        self.assertNotContains(response, "An unnecessary original echo")
        self.assertNotContains(response, "A timing comparison")

    def test_preview_and_complete_inline_pages_are_independent_of_shared_pages(self):
        self.publish(
            messages=[self.first, self.second],
            items=lambda inputs: (
                [
                    self.item(inputs, f"Single finding {number:02}", 0)
                    for number in range(36)
                ]
                + [
                    self.item(inputs, f"Shared finding {number:02}", 0, 1)
                    for number in range(30)
                ]
            ),
        )
        normal = self.client.get(self.reader_url(), {"list_page": 2})
        inline = self.analysis(normal, self.first)
        self.assertEqual(inline["count"], 36)
        self.assertEqual(len(inline["rows"]), 3)
        self.assertIsNone(inline["page_obj"])
        more = urlsplit(inline["more_url"])
        self.assertEqual(parse_qs(more.query)["message"], [str(self.first.pk)])
        self.assertEqual(parse_qs(more.query)["list_page"], ["2"])
        response = self.client.get(
            self.reader_url(),
            {
                "message": self.first.pk,
                "analysis_page": 2,
                "conversation_analysis_page": 2,
                "list_page": 2,
            },
        )
        inline = self.analysis(response, self.first)
        shared = response.context["conversation_analysis"]
        self.assertEqual(inline["page_obj"].number, 2)
        self.assertEqual(len(inline["rows"]), 11)
        self.assertIsNotNone(shared["page_obj"])
        self.assertEqual(shared["page_obj"].number, 2)
        self.assertEqual(len(shared["rows"]), 5)
        inline_previous = parse_qs(urlsplit(inline["previous_url"]).query)
        shared_previous = parse_qs(urlsplit(shared["previous_url"]).query)
        self.assertEqual(inline_previous["conversation_analysis_page"], ["2"])
        self.assertEqual(shared_previous["analysis_page"], ["2"])
        self.assertEqual(shared_previous["message"], [str(self.first.pk)])
        self.assertEqual(shared_previous["list_page"], ["2"])
        self.assertNotIn("analysis_page", response.context["page_query"])
        self.assertNotIn("message", response.context["page_query"])

    def test_preview_expansion_and_evidence_catalogue_agree_on_distinct_leads(self):
        key = "BOOK-7418"
        message = self.message(body=f"Confirm {key} before the pickup.")
        receipt = self.message(body=f"The receipt references {key}.")
        self.publish(
            messages=[message, receipt],
            items=lambda inputs: (
                [self.item(inputs, "Routine observation", 0) for _ in range(26)]
                + [
                    self.item(inputs, "An additional observation", 0),
                    self.item(
                        inputs,
                        key,
                        0,
                        label="Identifier candidate",
                        values={"raw": key},
                    ),
                    self.item(
                        inputs,
                        key,
                        1,
                        label="Identifier candidate",
                        values={"raw": key},
                    ),
                ]
            ),
        )
        preview = self.analysis(self.client.get(self.reader_url()), message)
        expanded = self.analysis(
            self.client.get(
                self.reader_url(), {"message": message.pk, "analysis_page": 1}
            ),
            message,
        )
        catalogue_url = reverse("core:analysis:browse", args=(self.case.pk,))
        filtered = {"kind": "text-message", "evidence": message.pk}
        first = self.client.get(catalogue_url, filtered)
        second = self.client.get(catalogue_url, filtered | {"page": 2})
        top = preview["rows"]
        self.assertEqual([row["item_index"] for row in top], [27, 0, 26])
        self.assertTrue(top[0]["review_reason"])
        self.assertContains(first, top[0]["review_reason"])
        for rows in (expanded["rows"], first.context["rows"]):
            self.assertEqual(
                [(row["id"], row["review_reason"]) for row in top],
                [(row["id"], row["review_reason"]) for row in rows[:3]],
            )
        all_rows = first.context["rows"] + second.context["rows"]
        self.assertCountEqual([row["item_index"] for row in all_rows], range(28))
        self.assertTrue(all(not row["review_reason"] for row in all_rows[3:]))

    @patch("core.analysis.ranking.MAX_MATCH_KEYS", 1)
    def test_offscreen_keys_cannot_consume_a_messages_review_budget(self):
        from phone.conversation_analysis import conversation_analysis_context
        from phone.conversations import pair_messages

        message = self.message(body="Pickup ticket BOOK-7418")
        receipt = self.message(body="Receipt BOOK-7418")
        earlier = self.message(body="Contact help@example.test")
        email_copy = self.message(body="Footer help@example.test")
        self.publish(
            messages=[message, receipt, earlier, email_copy],
            items=lambda inputs: [
                self.item(inputs, "Ordinary observation", 0),
                self.item(inputs, "BOOK-7418", 0, label="Identifier candidate"),
                self.item(inputs, "BOOK-7418", 1, label="Identifier candidate"),
                self.item(inputs, "help@example.test", 2, label="Email"),
                self.item(inputs, "help@example.test", 3, label="Email"),
            ],
        )
        scoped = pair_messages(self.case, self.person, self.correspondent)
        conversation_analysis_context(self.case, scoped, [message])
        top = message.analysis["rows"][0]
        self.assertEqual(top["text"], "BOOK-7418")
        self.assertTrue(top["review_reason"])
        self.assertFalse(message.analysis["load_on_open"])

        # Two competing keys on a full page defer unopened panels to their real
        # links, so clicking either message gives it the same independent budget.
        combined = self.client.get(self.reader_url())
        self.assertTrue(self.analysis(combined, message)["load_on_open"])
        self.assertContains(combined, "data-load-panel-on-open")
        for extra in ({}, {"analysis_page": 1}):
            opened = self.client.get(self.reader_url(), {"message": message.pk} | extra)
            analysis = self.analysis(opened, message)
            self.assertFalse(analysis["load_on_open"])
            promoted = analysis["rows"][0]
            self.assertEqual(
                (promoted["id"], promoted["review_reason"]),
                (top["id"], top["review_reason"]),
            )
            self.assertContains(opened, promoted["review_reason"])

    def test_old_export_link_shows_citations_across_the_conversation(self):
        export = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="First retained export",
            file=ContentFile(b"Synthetic source rows", name="synthetic.csv"),
        )
        CommunicationContext.objects.create(
            case=self.case,
            message=self.first,
            source_document=export,
            source_sha256=export.sha256,
            source_row=1,
            source_start=0,
            source_end=1,
            parser_version="synthetic-v1",
            timezone="America/Toronto",
            raw_sender="Synthetic source account",
        )
        self.publish(
            messages=[self.first, self.second],
            items=lambda inputs: (
                [
                    self.item(inputs, "A shared result across exports", 0, 1),
                    self.item(inputs, "Hidden message's own result", 1),
                ]
                + [self.item(inputs, f"Inline {number}", 0) for number in range(4)]
            ),
        )
        response = self.client.get(
            self.reader_url(), {"source": export.pk, "list_page": 2}
        )
        shared = response.context["conversation_analysis"]
        self.assertEqual(shared["count"], 1)
        row = shared["rows"][0]
        self.assertFalse(row["outside_sources"])
        self.assertEqual(len(row["message_sources"]), 2)
        targets = [
            parse_qs(urlsplit(source["url"]).query) for source in row["message_sources"]
        ]
        self.assertEqual(
            {q["message"][0] for q in targets},
            {str(self.first.pk), str(self.second.pk)},
        )
        for query in targets:
            self.assertNotIn("source", query)
            self.assertEqual(query["list_page"], ["2"])
        more = parse_qs(urlsplit(self.analysis(response, self.first)["more_url"]).query)
        self.assertNotIn("source", more)
        self.assertContains(response, "Hidden message&#x27;s own result")
        self.assertContains(response, "A shared result across exports")

    def test_shared_citation_links_find_exact_messages_beyond_the_loaded_page(self):
        instant = datetime(2026, 11, 10, 12, tzinfo=UTC)
        messages = [
            self.message(occurred_at=instant + timedelta(minutes=number))
            for number in range(52)
        ]
        self.publish(
            messages=[messages[0], messages[-1]],
            items=lambda inputs: [self.item(inputs, "A distant comparison", 0, 1)],
        )
        response = self.client.get(self.reader_url())
        self.assertNotIn(
            messages[-1].pk, {row.pk for row in response.context["page_obj"]}
        )
        row = response.context["conversation_analysis"]["rows"][0]
        target = next(
            source["url"]
            for source in row["message_sources"]
            if parse_qs(urlsplit(source["url"]).query)["message"]
            == [str(messages[-1].pk)]
        )
        linked = self.client.get(self.reader_url() + target)
        self.assertEqual(linked.context["page_obj"].number, 2)
        self.assertEqual(linked.context["selected_message"].pk, messages[-1].pk)

    def test_old_export_url_excludes_context_from_another_case(self):
        export = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Local retained export",
            file=ContentFile(b"Synthetic source row", name="synthetic.csv"),
        )
        context = CommunicationContext.objects.create(
            case=self.case,
            message=self.first,
            source_document=export,
            source_sha256=export.sha256,
            source_row=1,
            source_start=0,
            source_end=1,
            parser_version="synthetic-v1",
            timezone="America/Toronto",
            raw_sender="Synthetic source account",
        )
        self.publish()
        CommunicationContext.objects.filter(pk=context.pk).update(case=self.other_case)
        response = self.client.get(self.reader_url(), {"source": export.pk})
        self.assertNotContains(response, "Local retained export")
        self.assertEqual(self.analysis(response, self.first)["count"], 1)
        self.assertContains(response, "A saved observation")

    def test_latest_successful_results_keep_coverage_and_empty_success_supersedes(self):
        old = self.publish()
        self.publish(status="failed")
        initial = self.client.get(self.reader_url())
        self.assertEqual(self.analysis(initial, self.first)["count"], 1)
        latest = self.publish(
            status="partial",
            items=lambda inputs: [self.item(inputs, "Newest saved observation", 0)],
        )
        self.assertEqual(old.run.equivalence_key, latest.run.equivalence_key)
        AnalysisReview.objects.create(
            case=self.case,
            result=latest,
            reviewed_by=self.users["owner"],
            item_index=0,
            decision="rejected",
            comment="Retained investigator review reason",
        )
        AnalysisReview.objects.create(
            case=self.case,
            result=latest,
            reviewed_by=self.users["owner"],
            decision="unresolved",
            comment="The full result remains unresolved.",
        )
        response = self.client.get(self.reader_url())
        row = self.analysis(response, self.first)["rows"][0]
        self.assertEqual(row["text"], "Newest saved observation")
        self.assertTrue(row["partial"])
        self.assertContains(response, "Partial coverage")
        self.assertNotContains(response, "Retained investigator review reason")
        self.publish(items=[], status="empty")
        empty = self.client.get(self.reader_url())
        self.assertEqual(self.analysis(empty, self.first)["count"], 0)
        self.assertNotContains(empty, 'id="conversation-analysis"')
        self.assertNotContains(empty, "Newest saved observation")

    def test_changed_or_missing_run_sources_withhold_finding_content_and_links(self):
        self.publish(
            messages=[self.first, self.second],
            items=lambda inputs: [
                self.item(inputs, "Private derived wording", 0),
                self.item(inputs, "Private shared wording", 0, 1),
            ],
        )
        TextMessageEvidence.objects.filter(pk=self.second.pk).update(sha256="0" * 64)
        response = self.client.get(self.reader_url())
        inline = self.analysis(response, self.first)
        shared = response.context["conversation_analysis"]
        self.assertEqual(inline["count"], 1)
        for row in inline["rows"] + shared["rows"]:
            self.assertTrue(row["unavailable"])
            self.assertEqual(row["text"], "")
            self.assertEqual(row["sources"], [])
            self.assertEqual(row["message_sources"], [])
        self.assertNotContains(response, "Private derived wording")
        self.assertNotContains(response, "Private shared wording")
        self.second.delete()
        missing = self.client.get(self.reader_url())
        self.assertTrue(self.analysis(missing, self.first)["rows"][0]["unavailable"])
        self.assertNotContains(missing, "Private derived wording")

    def test_roles_revocation_and_cross_case_references_never_start_work(self):
        self.publish()
        count = AnalysisRun.objects.count()
        for role in ("owner", "editor", "viewer"):
            self.login(role)
            response = self.client.get(self.reader_url())
            self.assertContains(response, "A saved observation")
            self.assertEqual(self.client.post(self.reader_url()).status_code, 405)
        self.assertEqual(AnalysisRun.objects.count(), count)
        foreign_person = Person.objects.create(
            case=self.other_case, first_name="Foreign"
        )
        foreign_message = self.message(
            case=self.other_case,
            uploaded_by=self.users["outsider"],
            sender=foreign_person,
            recipient=foreign_person,
        )
        foreign = self.publish(
            case=self.other_case,
            messages=[foreign_message],
            items=lambda inputs: [self.item(inputs, "Foreign finding content", 0)],
        )
        # A corrupt projection cannot turn a foreign result into a local finding.
        AnalysisFindingSource.objects.filter(finding__result=foreign).update(
            case=self.case, evidence_id=self.first.pk
        )
        self.login("viewer")
        response = self.client.get(self.reader_url())
        self.assertNotContains(response, "Foreign finding content")
        self.assertEqual(
            self.client.get(
                self.reader_url(), {"message": foreign_message.pk}
            ).status_code,
            404,
        )
        CaseMembership.objects.filter(
            case=self.case, user=self.users["viewer"]
        ).delete()
        self.assertEqual(self.client.get(self.reader_url()).status_code, 404)
        self.login("outsider")
        self.assertEqual(self.client.get(self.reader_url()).status_code, 404)

    def test_archived_sources_withhold_findings_and_active_output_is_escaped(self):
        self.publish(
            items=lambda inputs: [
                self.item(inputs, "<script>alert('untrusted')</script>", 0)
            ]
        )
        response = self.client.get(self.reader_url())
        self.assertEqual(self.analysis(response, self.first)["count"], 1)
        self.assertContains(
            response, "&lt;script&gt;alert(&#x27;untrusted&#x27;)&lt;/script&gt;"
        )
        self.assertNotContains(response, "<script>alert('untrusted')</script>")
        TextMessageEvidence.objects.filter(pk=self.first.pk).update(
            is_archived=True,
            archive_changed_at=timezone.now(),
            archive_changed_by=self.users["owner"],
        )
        response = self.client.get(self.reader_url())
        self.assertNotIn(self.first, response.context["page_obj"])
        self.assertNotContains(response, "&lt;script&gt;alert")

    def test_long_finding_text_and_shared_citations_have_bounded_previews(self):
        messages = [self.first, self.second] + [self.message() for _number in range(3)]
        long_text = "Detailed retained finding. " * 80
        result = self.publish(
            messages=messages,
            items=lambda inputs: [
                self.item(inputs, long_text, 0),
                self.item(inputs, "Comparison of five messages", *range(5)),
            ],
        )
        response = self.client.get(self.reader_url())
        inline = self.analysis(response, self.first)
        row = inline["rows"][0]
        self.assertLessEqual(len(row["preview"]), 300)
        self.assertEqual(row["text"], long_text.strip()[:1000])
        self.assertTrue(row["text_truncated"])
        self.assertIn(str(result.run_id), row["url"])
        shared = response.context["conversation_analysis"]["rows"][0]
        self.assertEqual(len(shared["message_sources"]), 3)
        self.assertEqual(shared["additional_message_sources"], 2)
        self.assertFalse(shared["outside_sources"])
        self.assertIn(str(result.run_id), shared["url"])

    def test_visible_findings_are_batched_without_full_result_payload_reads(self):
        from phone.conversation_analysis import conversation_analysis_context
        from phone.conversations import pair_messages

        messages = [self.first] + [self.message() for _number in range(29)]
        self.publish(
            messages=messages,
            items=lambda inputs: [
                self.item(inputs, f"Observation for source {number}", number)
                for number in range(len(inputs))
            ],
        )
        scoped = pair_messages(self.case, self.person, self.correspondent)
        with CaptureQueriesContext(connection) as one:
            conversation_analysis_context(self.case, scoped, messages[:1])
        with CaptureQueriesContext(connection) as many:
            conversation_analysis_context(self.case, scoped, messages)
        self.assertLessEqual(
            len(many), len(one) + 2, "Finding queries grew per message"
        )
        self.assertTrue(all(message.analysis["count"] == 1 for message in messages))
        sql = "\n".join(query["sql"] for query in many)
        self.assertNotIn('"core_analysisresult"."provenance"', sql)
        self.assertNotRegex(sql, r'SELECT "core_analysisresult"\."id", .*"data"')
        self.assertNotIn(', "phone_textmessageevidence"."body",', sql)

    def test_message_removed_after_reader_selection_fails_recoverably(self):
        from phone.conversation_analysis import conversation_analysis_context
        from phone.conversations import pair_messages

        self.publish()
        selected = self.first
        messages = pair_messages(self.case, self.person, self.correspondent)
        # The reader retains this object, while a later query sees its removal.
        TextMessageEvidence.objects.filter(pk=selected.pk).delete()
        with self.assertRaises(Http404):
            conversation_analysis_context(
                self.case,
                messages,
                [selected],
                selected=selected,
                params={"analysis_page": 1},
            )

    def test_full_message_page_with_mixed_findings_stays_within_query_budget(self):
        messages = [self.first, self.second] + [self.message() for _number in range(48)]
        export = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic retained conversation export",
            file=ContentFile(b"Synthetic source rows", name="synthetic.csv"),
        )
        for number, message in enumerate(messages, 1):
            CommunicationContext.objects.create(
                case=self.case,
                message=message,
                source_document=export,
                source_sha256=export.sha256,
                source_row=number,
                source_start=number - 1,
                source_end=number,
                parser_version="synthetic-v1",
                timezone="America/Toronto",
                raw_sender="Synthetic source account",
            )
        self.publish(
            items=lambda inputs: [
                self.item(inputs, f"Single finding {number:02}", 0)
                for number in range(28)
            ],
        )
        self.publish(
            messages=[self.first, messages[-1]],
            items=lambda inputs: [
                self.item(inputs, f"Shared finding {number:02}", 0, 1)
                for number in range(27)
            ],
        )
        # Limit only these reads; a failed query rolls this local setting back
        # with the savepoint, and successful reads restore the original value.
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SHOW statement_timeout")
            previous_timeout = cursor.fetchone()[0]
            cursor.execute("SET LOCAL statement_timeout = 5000")
            preview = self.client.get(
                self.reader_url(), {"source": export.pk, "list_page": 2}
            )
            expanded = self.client.get(
                self.reader_url(),
                {
                    "message": self.first.pk,
                    "source": export.pk,
                    "list_page": 2,
                    "analysis_page": 1,
                    "conversation_analysis_page": 1,
                },
            )
            cursor.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                [previous_timeout],
            )
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(len(preview.context["page_obj"]), 50)
        self.assertEqual(len(self.analysis(preview, self.first)["rows"]), 3)
        shared_preview = preview.context["conversation_analysis"]
        self.assertEqual(shared_preview["count"], 27)
        self.assertEqual(len(shared_preview["rows"]), 3)
        self.assertIsNone(shared_preview["page_obj"])
        self.assertEqual(
            parse_qs(urlsplit(shared_preview["more_url"]).query),
            {"list_page": ["2"], "conversation_analysis_page": ["1"]},
        )
        self.assertEqual(expanded.status_code, 200)
        inline = self.analysis(expanded, self.first)
        shared = expanded.context["conversation_analysis"]
        self.assertEqual(inline["count"], 28)
        self.assertEqual(len(inline["rows"]), 25)
        self.assertTrue(inline["page_obj"].has_next())
        self.assertEqual(shared["count"], 27)
        self.assertEqual(len(shared["rows"]), 25)
        self.assertTrue(shared["page_obj"].has_next())

    def test_result_superseded_between_preview_and_finding_read_fails_recoverably(self):
        from phone.conversation_analysis import conversation_analysis_context
        from phone.conversations import pair_messages

        initial = self.publish()
        messages = pair_messages(self.case, self.person, self.correspondent)
        published = False

        def supersede_before_finding_read(execute, sql, params, many, context):
            nonlocal published
            if not published and "jsonb_extract_path(" in sql:
                published = True
                latest = self.publish(items=[], status="empty")
                self.assertEqual(
                    initial.run.equivalence_key, latest.run.equivalence_key
                )
            return execute(sql, params, many, context)

        with connection.execute_wrapper(supersede_before_finding_read):
            with self.assertRaises(Http404):
                conversation_analysis_context(self.case, messages, [self.first])
        self.assertTrue(
            published, "The new result must publish after preview selection"
        )
