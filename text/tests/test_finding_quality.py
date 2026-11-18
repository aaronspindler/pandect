from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from django.test import SimpleTestCase

from core.analysis.sources import Source
from phone.analysis.ai import semantic_cross_channel_links
from text.analysis.ai import content_events


def suggestion(kind, text, *citations, status="reported", **attributes):
    return {
        "kind": kind,
        "text": text,
        "status": status,
        "citations": [
            {"source_id": source.id, "quote": source.text} for source in citations
        ],
        **attributes,
    }


class CitedFindingQualityTests(SimpleTestCase):
    def setUp(self):
        self.case_id, self.sender, self.recipient = uuid4(), uuid4(), uuid4()
        self.start = datetime(2026, 10, 27, 12, tzinfo=UTC)

    def message(self, text, minutes=0):
        return Source(
            str(uuid4()),
            "text-message",
            SimpleNamespace(
                pk=uuid4(),
                case_id=self.case_id,
                body=text,
                sender_id=self.sender,
                recipient_id=self.recipient,
                occurred_at=self.start + timedelta(minutes=minutes),
            ),
        )

    def test_claims_keep_material_qualifiers_and_distinct_cited_passages(self):
        first = self.message("The delivery was planned for noon.")
        second = self.message("The noon delivery was cancelled.")
        acknowledgment = self.message("I know.")
        cancellation = suggestion(
            "cancellation",
            "The sender cancelled the noon delivery.",
            first,
            second,
            status="cancelled",
        )
        candidates = [
            cancellation,
            cancellation
            | {
                "text": "the sender cancelled  the noon delivery.",
                "citations": cancellation["citations"][::-1],
            },
            cancellation | {"status": "cancellation conditional"},
            cancellation | {"citations": cancellation["citations"][1:]},
            suggestion(
                "factual_claim", "The sender confirmed delivery.", acknowledgment
            ),
            suggestion("prediction", "The sender expects improvement.", first),
            suggestion("factual_claim", "I know.", second),
            *(
                suggestion(
                    "factual_claim",
                    "The sender refers to an unnamed action.",
                    second,
                    status=status,
                )
                for status in ("ambiguous", "tentative_reference", "", None)
            ),
        ]
        result = content_events(
            [first, second, acknowledgment],
            {},
            infer=lambda prompt, schema: {"items": candidates},
        )
        self.assertEqual(
            [item.get("report_only", False) for item in result["items"]],
            [False, True, False, False, True, True, True, True, True, True, True],
        )
        self.assertEqual(
            result["items"][4]["anchors"],
            [acknowledgment.anchor(start=0, end=len(acknowledgment.text))],
        )

    def test_links_require_independent_originals_and_specific_support(self):
        question = self.message("Did you cancel the noon delivery?")
        answer = self.message("Yes.", 100)
        copied = self.message(question.text, 200)
        derived = replace(
            question,
            id=str(uuid4()),
            extracted_text="The noon delivery was cancelled.",
            derived_result_id=str(uuid4()),
        )
        candidates = [
            suggestion(
                "question_answer",
                "The respondent confirmed cancellation.",
                question,
                answer,
            ),
            suggestion(
                "explicit_reference",
                "The pair references the noon delivery.",
                question,
                answer,
                target="noon delivery",
                scope="specific_event",
            ),
            suggestion(
                "shared_reported_event",
                "The pair names the noon delivery.",
                question,
                copied,
                target="noon delivery",
                scope="specific_event",
            ),
            suggestion(
                "shared_reported_event",
                "The pair names the noon delivery.",
                question,
                derived,
                target="noon delivery",
                scope="specific_event",
            ),
            suggestion(
                "continued_topic", "The pair discusses delivery.", question, derived
            ),
            suggestion("explicit_reference", "The reply refers to delivery.", question),
        ]
        result = semantic_cross_channel_links(
            [question, answer, copied, derived],
            {"window_minutes": 0},
            infer=lambda prompt, schema: {"items": candidates},
        )
        model_items = [item for item in result["items"] if "status" in item["values"]]
        self.assertEqual(
            [item.get("report_only", False) for item in model_items],
            [False, True, True, True, True, True],
        )
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(result["metrics"]["unresolved_semantic_suggestions"], 1)

    def test_claim_targets_are_specific_and_present_in_the_quote_and_suggestion(self):
        event = self.message("The noon delivery took place at the south gate.")
        denial = self.message("I never received the package listed in CASE-1234.")
        correction = self.message(
            "I retract the claim that CASE-1234 had already been closed."
        )
        general = self.message("Every relationship in my adult life began in winter.")
        opinion = self.message("I think the shipping contractor seems overrated.")
        apparent_match = self.message("The afternoon delivery was delayed.")
        specific = suggestion(
            "reported_event",
            "The sender reports the noon delivery took place.",
            event,
            target="noon delivery",
            scope="specific_event",
        )
        candidates = [
            specific,
            suggestion(
                "factual_claim",
                "The sender denies receiving the package listed in case-1234.",
                denial,
                target="CASE-1234",
                scope="specific_event",
            ),
            suggestion("correction", correction.text, correction),
            suggestion(
                "factual_claim",
                general.text,
                general,
                target="adult life",
                scope="general_history",
            ),
            suggestion(
                "factual_claim",
                opinion.text,
                opinion,
                target="shipping contractor",
                scope="personal_evaluation",
            ),
            specific | {"scope": None},
            specific | {"text": "The sender says a delivery took place."},
            specific | {"target": "the noon"},
            specific
            | {
                "citations": suggestion("reported_event", "", apparent_match)[
                    "citations"
                ]
            },
        ]
        result = content_events(
            [event, denial, correction, general, opinion, apparent_match],
            {},
            infer=lambda prompt, schema: {"items": candidates},
        )
        self.assertEqual(
            [item.get("report_only", False) for item in result["items"]],
            [False, False, False, True, True, True, True, True, True],
        )
        self.assertEqual(result["items"][3]["values"]["scope"], "general_history")
        self.assertEqual(result["items"][3]["values"]["target"], "adult life")

    def test_semantic_link_needs_the_same_literal_target_and_an_explicit_reference(
        self,
    ):
        first = self.message("The noon delivery is tracked in CASE-1234.")
        second = self.message("I closed CASE-1234 after receiving the package.", 100)
        vague = self.message("I closed that record after receiving the package.", 200)
        link = suggestion(
            "explicit_reference",
            "Both sources explicitly identify CASE-1234.",
            first,
            second,
            target="CASE-1234",
            scope="specific_action",
        )
        candidates = [
            link,
            link
            | {"kind": "shared_reported_event", "text": "Both reports name CASE-1234."},
            link
            | {"text": "Both reports mention CASE-1234, which was probably cancelled."},
            link | {"text": "The closing message appears to refer to CASE-1234."},
            link | {"text": "The closing message probably refers to CASE-1234."},
            link | {"text": "The closing message may reference CASE-1234."},
            link
            | {"text": "The closing message appears to be a reference to CASE-1234."},
            link | {"text": "The two messages identify the same named record."},
            link
            | {
                "citations": suggestion("explicit_reference", "", first, vague)[
                    "citations"
                ]
            },
            link | {"scope": "general_history"},
            link | {"scope": ["specific_action"]},
            link
            | {
                "kind": "correction",
                "text": "The reports contrast the status of CASE-1234.",
            },
        ]
        result = semantic_cross_channel_links(
            [first, second, vague],
            {"window_minutes": 0},
            infer=lambda prompt, schema: {"items": candidates},
        )
        self.assertEqual(
            [item.get("report_only", False) for item in result["items"]],
            [False, False, False, True, True, True, True, True, True, True, True, True],
        )
