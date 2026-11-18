"""Findings show their useful content once and keep supporting detail inspectable."""

from copy import deepcopy
from types import SimpleNamespace

from django.template.loader import render_to_string
from django.test import TestCase
from django.utils import timezone

from core.analysis.presentation import measurement_formatter
from core.analysis.result_views import result_view
from core.analysis.sources import Source
from core.testing import EvidenceTestMixin
from people.models import Person
from phone.models import TextMessageEvidence


class FindingLayoutTests(EvidenceTestMixin, TestCase):
    def render_finding(
        self, operation, values, *, text="", label="Finding", sources=(), query=None
    ):
        formatter = measurement_formatter(
            values, case=self.case, sources=sources, query=query
        )
        item = {
            "index": 0,
            "number": 1,
            "label": label,
            "url": "/finding/",
            "text": text,
            "values": deepcopy(values),
            "measurements": formatter(values, duplicate_text=text),
            "sources": [
                {
                    "id": source.id,
                    "label": source.item.title,
                    "kind_label": "Text message",
                    "url": f"/source/{index}/",
                    "excerpt": source.text[:320]
                    + ("…" if len(source.text) > 320 else ""),
                    "anchors": [{"start": 0, "end": len(source.text)}],
                }
                for index, source in enumerate(sources)
            ],
        }
        result_view(operation, [item], formatter, query=query)
        content = render_to_string(
            "core/analysis/finding.html",
            {
                "item": item,
                "run": SimpleNamespace(operation=operation),
                "metadata_result": operation in {"image_metadata", "audio_metadata"},
            },
        )
        return item, content

    def messages(self, *, first_text="The delivery is tomorrow."):
        recipient = Person.objects.create(case=self.case, first_name="Recipient")
        first = self.create_evidence(
            TextMessageEvidence,
            title="Message one",
            body=first_text,
            recipient=recipient,
        )
        second = self.create_evidence(
            TextMessageEvidence,
            title="Message two",
            body="Understood.",
            sender=recipient,
        )
        return [
            Source(str(message.pk), "text-message", message)
            for message in (first, second)
        ]

    def sequence_values(self, sources):
        before, after = (
            {
                "source_id": source.id,
                "channel": "text-message",
                "sender_id": str(source.item.sender_id),
                "recipient_id": str(source.item.recipient_id),
                "occurred_at": f"2026-07-30T01:23:{10 + index * 16:02d}+00:00",
                "text": source.text,
                "native_message_id": f"native-{index}",
                "reply_to_id": "",
            }
            for index, source in enumerate(sources)
        )
        return {
            "before": before,
            "after": after,
            "elapsed_seconds": 16.0,
            "shared_words": [],
            "basis": (
                "temporal adjacency; lexical overlap is a separate weak content cue"
            ),
            "interpretation": (
                "Unresolved: proximity in time or subject wording does not prove "
                "the same event, a reference, discussion content, or causation."
            ),
        }

    def test_message_sequence_displays_each_message_once_with_localized_time(self):
        sources = self.messages()
        values = self.sequence_values(sources)
        values["before"]["retained_transport_note"] = "Delivery status unavailable"
        original = deepcopy(values)
        with timezone.override("America/Toronto"):
            item, content = self.render_finding(
                "conversation_link",
                values,
                text="Recorded temporal adjacency",
                label="Candidate exchange sequence",
                sources=sources,
            )
        self.assertEqual(content.count(sources[0].text), 1)
        self.assertEqual(content.count(sources[1].text), 1)
        self.assertIn("Messages 16 seconds apart", content)
        self.assertIn("29 Jul 2026, 21:23:10 EDT", content)
        self.assertIn('datetime="2026-07-30T01:23:10+00:00"', content)
        visible, details = content.split('<details class="analysis-finding-details', 1)
        self.assertIn("Timing alone does not establish", visible)
        self.assertIn("Reported sender", visible)
        self.assertNotIn("Shared words", visible)
        self.assertNotIn("native-0", visible)
        self.assertIn("native-0", details)
        self.assertIn("Delivery status unavailable", details)
        self.assertIn("Characters 0–", details)
        self.assertNotIn("Before", [row["label"] for row in item["measurements"]])
        self.assertEqual(values, original)

    def test_long_messages_do_not_repeat_truncated_source_excerpts(self):
        body = "Retained source text. " * 30
        sources = self.messages(first_text=body)
        _, content = self.render_finding(
            "conversation_link",
            self.sequence_values(sources),
            label="Candidate exchange sequence",
            sources=sources,
        )
        self.assertEqual(content.count(body), 1)
        self.assertNotIn(body[:320] + "…", content)

    def test_invalid_elapsed_values_do_not_become_factual_headings(self):
        sources = self.messages()
        for elapsed in (-1, float("nan"), float("inf"), None, "16", True):
            with self.subTest(elapsed=elapsed):
                values = self.sequence_values(sources) | {"elapsed_seconds": elapsed}
                item, content = self.render_finding(
                    "conversation_link",
                    values,
                    label="Candidate exchange sequence",
                    sources=sources,
                )
                self.assertEqual(item["label"], "Candidate exchange sequence")
                self.assertNotIn("seconds apart", content)
                self.assertIn("Elapsed seconds", content)

    def test_shared_words_and_unfamiliar_interpretations_remain_visible(self):
        sources = self.messages()
        values = self.sequence_values(sources) | {
            "shared_words": ["delivery"],
            "interpretation": "This export omits some exchanges.",
        }
        _, content = self.render_finding(
            "conversation_link",
            values,
            label="Candidate exchange sequence",
            sources=sources,
        )
        visible = content.split('<details class="analysis-finding-details', 1)[0]
        self.assertIn("Shared words", visible)
        self.assertIn("delivery", visible)
        self.assertIn("This export omits some exchanges.", visible)

    def test_missing_message_source_keeps_unrendered_payload_inspectable(self):
        sources = self.messages()
        values = self.sequence_values(sources)
        item, content = self.render_finding(
            "conversation_link", values, sources=sources[:1]
        )
        self.assertEqual(len(item["messages"]), 1)
        self.assertIn("After", [row["label"] for row in item["measurements"]])
        self.assertIn("Understood.", content)

    def test_critical_context_precedes_collapsed_supporting_fields(self):
        for operation in (
            "content_events",
            "native_text",
            "transcription",
            "proximity",
        ):
            with self.subTest(operation=operation):
                _, content = self.render_finding(
                    operation,
                    {
                        "status": "unresolved",
                        "coverage": "Two selected sources only",
                        "interpretation": "This does not verify the event.",
                        "sample_count": 0,
                        "truncated": False,
                        "missing_duration": None,
                    },
                    text="A reported observation",
                )
                visible, details = content.split(
                    '<details class="analysis-finding-details', 1
                )
                self.assertIn("Two selected sources only", visible)
                self.assertIn("This does not verify the event.", visible)
                self.assertIn("unresolved", visible)
                self.assertNotIn("Sample count", visible)
                self.assertIn("Sample count", details)
                self.assertRegex(details, r">0\s*<")
                self.assertIn("No", details)
                self.assertIn("Unknown", details)

    def test_geographic_subject_remains_visible_beside_the_finding(self):
        _, content = self.render_finding(
            "candidate_visits",
            {"Subject association": f"person:{self.person.pk}", "parser_option": 4},
            text="An observed visit interval.",
        )
        visible, details = content.split('<details class="analysis-finding-details', 1)
        self.assertIn(str(self.person), visible)
        self.assertNotIn("Parser option", visible)
        self.assertIn("Parser option", details)

    def test_writing_comparison_examples_remain_visible_with_source_links(self):
        sources = self.messages()
        _, content = self.render_finding(
            "style_change",
            {
                "supporting_examples": [
                    {
                        "source_id": sources[0].id,
                        "excerpt": "delivery",
                        "start": 4,
                        "end": 12,
                    }
                ],
                "contrary_examples": [],
            },
            text="A difference in wording.",
            sources=sources,
        )
        visible, details = content.split('<details class="analysis-finding-details', 1)
        self.assertIn("Supporting examples", visible)
        self.assertIn("<blockquote>delivery</blockquote>", visible)
        self.assertIn(sources[0].url, visible)
        self.assertIn("Contrary examples", details)

    def test_no_primary_content_keeps_structured_values_visible(self):
        _, content = self.render_finding("style_profiles", {"Legacy measurement": 4})
        self.assertIn("Legacy measurement", content)
        self.assertNotIn('class="analysis-finding-details', content)

    def test_decoded_metadata_stays_in_its_readable_table(self):
        _, content = self.render_finding(
            "image_metadata", {"Decoded width": 1024, "Color mode": "RGB"}
        )
        self.assertIn("analysis-metadata-table", content)
        self.assertIn("1024", content)
        self.assertIn("RGB", content)
        self.assertNotIn('class="analysis-finding-details', content)

    def test_requested_nested_value_pages_reopen_only_their_details(self):
        for values in (
            {"supporting_details": {"observed_values": list(range(205))}},
            {"supporting_examples": [{"observed_values": list(range(205))}]},
        ):
            with self.subTest(values=next(iter(values))):
                item, _ = self.render_finding(
                    "style_profiles", values, text="Observation"
                )
                row = item["measurements"][0]
                children = row.get("children") or row["examples"][0]["children"]
                anchor = children[0]["pagination"]["anchor"]
                for page in ("2", "1"):
                    _, content = self.render_finding(
                        "style_profiles",
                        values,
                        text="Observation",
                        query={anchor: page},
                    )
                    self.assertIn(
                        '<details class="analysis-finding-details disclosure" open>',
                        content,
                    )
                    self.assertIn(f'id="{anchor}"', content)
                _, content = self.render_finding(
                    "style_profiles",
                    values,
                    text="Observation",
                    query={"values_another_finding": "2"},
                )
                self.assertIn(
                    '<details class="analysis-finding-details disclosure">', content
                )

    def test_requested_message_reference_page_reopens_details(self):
        sources = self.messages()
        values = self.sequence_values(sources)
        values["before"]["audit_values"] = list(range(205))
        item, _ = self.render_finding("conversation_link", values, sources=sources)
        reference = next(
            row
            for row in item["message_details"][0]["references"]
            if row["label"] == "Audit values"
        )
        anchor = reference["pagination"]["anchor"]
        _, content = self.render_finding(
            "conversation_link", values, sources=sources, query={anchor: "2"}
        )
        self.assertIn(
            '<details class="analysis-finding-details disclosure" open>', content
        )
        self.assertIn("201–205 of 205", content)
