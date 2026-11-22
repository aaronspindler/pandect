from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from core.analysis.review_signals import classify_review_signal_v1
from core.analysis.sources import Source
from phone.analysis.ai import session_summaries
from phone.analysis.deterministic.conversations import (
    conversation_sessions,
    interaction_patterns,
)
from phone.analysis.workflows import cross_channel_links
from phone.message_csv import IMESSAGE_PARSER_VERSION, WHATSAPP_PARSER_VERSION


class ConversationAnalysisTests(SimpleTestCase):
    def setUp(self):
        self.person_a = uuid4()
        self.person_b = uuid4()
        self.person_c = uuid4()
        self.case_id = uuid4()
        self.start = datetime(2026, 10, 27, 12, tzinfo=UTC)

    def message(
        self,
        body,
        minutes=0,
        *,
        kind="text-message",
        sender=None,
        recipient=None,
        timed=True,
    ):
        item = SimpleNamespace(
            pk=uuid4(),
            case_id=self.case_id,
            body=body,
            sender_id=sender or self.person_a,
            recipient_id=recipient or self.person_b,
            occurred_at=self.start + timedelta(minutes=minutes) if timed else None,
        )
        return Source(str(uuid4()), kind, item)

    def test_sessions_order_boundaries_adjustments_and_native_distinction(self):
        first = self.message("Hello, can you confirm the plan?")
        reply = self.message(
            "Yes, the plan is confirmed.",
            1,
            sender=self.person_b,
            recipient=self.person_a,
        )
        later = self.message("A new discussion", 200)
        result = conversation_sessions([later, reply, first], {})
        self.assertEqual(result["metrics"]["sessions"], 2)
        self.assertEqual(result["items"][0]["source_ids"], [first.id, reply.id])
        self.assertIn("inferred", result["items"][0]["values"]["grouping"])
        self.assertEqual(result["items"][0]["values"]["native_thread_references"], [])
        joined = conversation_sessions(
            [first, reply, later], {"join_before": [later.id]}
        )
        self.assertEqual(joined["metrics"]["sessions"], 1)
        self.assertTrue(joined["items"][0]["values"]["investigator_adjusted"])
        broken = conversation_sessions([first, reply], {"break_before": [reply.id]})
        self.assertEqual(broken["metrics"]["sessions"], 2)

    def test_missing_equal_times_and_mixed_participants(self):
        a = self.message("First")
        b = self.message("Equal time", sender=self.person_b, recipient=self.person_a)
        c = self.message("Other pair", recipient=self.person_c)
        unknown = self.message("Unknown time", timed=False)
        result = conversation_sessions([unknown, c, b, a], {})
        self.assertEqual(result["metrics"]["sessions"], 3)
        self.assertEqual(result["metrics"]["missing_event_times"], 1)
        self.assertTrue(any("unresolved" in x["text"] for x in result["items"]))
        with self.assertRaises(ValidationError):
            conversation_sessions([a], {"break_before": ["not-selected"]})

    def test_native_context_preserved_without_becoming_inferred_identity(self):
        a = self.message("Group note")
        a.item.communication_context = SimpleNamespace(
            participants=["account-A", "account-B", "account-C"],
            source_document_id=uuid4(),
            native_message_id="message-source-1",
            native_thread_id="source-thread",
            reply_to_id="earlier-message",
        )
        result = conversation_sessions([a], {})
        row = result["items"][0]["values"]["messages"][0]
        self.assertEqual(row["native_message_id"], "message-source-1")
        self.assertEqual(row["reply_to_id"], "earlier-message")
        self.assertEqual(
            len(result["items"][0]["values"]["native_thread_references"]), 1
        )

    def test_interaction_counts_response_denominators_and_coverage(self):
        a = self.message("Question?")
        b = self.message("Answer", 2, sender=self.person_b, recipient=self.person_a)
        c = self.message(
            "Another note", 3, sender=self.person_b, recipient=self.person_a
        )
        d = self.message("Later exchange", 300)
        missing = self.message("Missing time", timed=False)
        result = interaction_patterns(
            [a, b, c, d, missing],
            {"compare_split_at": (self.start + timedelta(minutes=10)).isoformat()},
        )
        metrics = result["metrics"]
        self.assertEqual(metrics["records"], 5)
        self.assertEqual(metrics["timed_records"], 4)
        self.assertEqual(metrics["missing_event_times"], 1)
        self.assertEqual(metrics["response_intervals_seconds"], [120.0])
        self.assertEqual(
            sum(metrics["apparent_session_initiations_by_sender"].values()), 2
        )
        self.assertEqual(len([x for x in result["items"] if "split" in x["label"]]), 2)
        with self.assertRaises(ValidationError):
            interaction_patterns([a], {"compare_split_at": "2026-10-27T12:00:00"})

    def test_interaction_statistics_reuse_adjusted_and_content_session_boundaries(self):
        a = self.message("Question?")
        b = self.message("Answer", 2, sender=self.person_b, recipient=self.person_a)
        later = self.message("Later exchange", 300)
        parameters = {"break_before": [b.id], "join_before": [later.id]}
        sessions = conversation_sessions([a, b, later], parameters)
        patterns = interaction_patterns([a, b, later], parameters)
        observed = [
            item
            for item in patterns["items"]
            if item["label"].startswith("Observed session")
        ]
        self.assertEqual(
            [item["source_ids"] for item in observed],
            [item["source_ids"] for item in sessions["items"]],
        )
        self.assertEqual(patterns["metrics"]["response_intervals_seconds"], [298 * 60])
        farewell = self.message("Goodbye")
        greeting = self.message(
            "Hello", 6, sender=self.person_b, recipient=self.person_a
        )
        self.assertEqual(
            interaction_patterns([farewell, greeting], {})["metrics"][
                "response_interval_count"
            ],
            0,
        )

    def test_comparison_period_fragment_does_not_invent_session_initiation(self):
        a = self.message("Question?")
        b = self.message("Answer", 2, sender=self.person_b, recipient=self.person_a)
        result = interaction_patterns(
            [a, b],
            {"compare_split_at": (self.start + timedelta(minutes=1)).isoformat()},
        )
        after = next(
            item
            for item in result["items"]
            if item["label"] == "At/after selected split"
        )
        self.assertEqual(after["values"]["apparent_session_initiations_by_sender"], {})
        self.assertEqual(after["values"]["inferred_session_numbers"], [1])
        self.assertEqual(after["values"]["contributing_source_ids"], [b.id])

    def test_self_messages_never_demonstrate_other_person_response(self):
        a = self.message("Self one", sender=self.person_a, recipient=self.person_a)
        b = self.message("Self two", 1, sender=self.person_a, recipient=self.person_a)
        result = interaction_patterns([a, b], {})
        self.assertEqual(result["metrics"]["observed_turn_changes"], 0)
        self.assertIsNone(result["metrics"]["median_response_interval_seconds"])

    def test_crosschannel_metadata_only_call_has_no_content_claim(self):
        a = self.message("Package delivery remains uncertain")
        call = self.message(
            "", 2, kind="phone-call", sender=self.person_b, recipient=self.person_a
        )
        b = self.message("Package delivery next week", 4)
        result = cross_channel_links([a, call, b], {})
        self.assertEqual(len(result["items"]), 2)
        self.assertTrue(all(x["values"]["shared_words"] == [] for x in result["items"]))
        self.assertIn("only", " ".join(result["warnings"]))
        self.assertTrue(
            any(
                x["values"]["before"]["text"] == ""
                or x["values"]["after"]["text"] == ""
                for x in result["items"]
            )
        )

    def test_crosschannel_form_window_alias_is_honored(self):
        a = self.message("First")
        b = self.message("Second", 2, kind="phone-call")
        self.assertEqual(
            cross_channel_links([a, b], {"time_window_minutes": 1})["outcome"], "empty"
        )
        self.assertEqual(
            cross_channel_links([a, b], {"time_window_minutes": 3})["metrics"][
                "window_minutes"
            ],
            3,
        )

    def test_whatsapp_reply_signal_is_scoped_to_its_retained_export(self):
        original = self.message("First")
        reply = self.message("Second", 1, sender=self.person_b, recipient=self.person_a)
        export, other_export = uuid4(), uuid4()
        for left_version, right_version, reply_export, expected in (
            (WHATSAPP_PARSER_VERSION, WHATSAPP_PARSER_VERSION, export, "native_reply"),
            (WHATSAPP_PARSER_VERSION, WHATSAPP_PARSER_VERSION, other_export, ""),
            (WHATSAPP_PARSER_VERSION, IMESSAGE_PARSER_VERSION, other_export, ""),
            (IMESSAGE_PARSER_VERSION, WHATSAPP_PARSER_VERSION, other_export, ""),
        ):
            with self.subTest(
                before=left_version, after=right_version, reply_export=reply_export
            ):
                original.item.communication_context = SimpleNamespace(
                    parser_version=left_version,
                    source_document_id=export,
                    native_message_id="message-1",
                )
                reply.item.communication_context = SimpleNamespace(
                    parser_version=right_version,
                    source_document_id=reply_export,
                    reply_to_id="message-1",
                )
                (candidate,) = cross_channel_links([original, reply], {})["items"]
                self.assertEqual(
                    candidate["values"]["after"]["reply_to_id"], "message-1"
                )
                self.assertEqual(
                    classify_review_signal_v1("conversation_link", candidate), expected
                )

    def test_observed_crosschannel_links_do_not_request_inference(self):
        a = self.message("The harbour proposal.")
        b = self.message("Which harbour proposal?", 1)
        with patch("phone.analysis.ai.cited_model_analysis") as inference:
            result = cross_channel_links([a, b], {})
        inference.assert_not_called()
        self.assertEqual(result["outcome"], "complete")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(
            result["items"][0]["values"]["shared_words"], ["harbour", "proposal"]
        )
        self.assertEqual(result["metrics"]["semantic_link_candidates"], 0)
        self.assertEqual(result["metrics"]["unresolved_semantic_suggestions"], 0)

    def test_semantic_link_failure_is_explicit_partial_metadata(self):
        a = self.message("that place")
        b = self.message(
            "Which place?", 1, sender=self.person_b, recipient=self.person_a
        )
        result = cross_channel_links([a, b], {"semantic_links": True}, infer=None)
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(len(result["items"]), 1)

    def test_semantic_links_require_two_distinct_cited_sources(self):
        a = self.message("The harbour proposal.")
        b = self.message("Which earlier harbour proposal?", 300)

        def suggestion(citations):
            return {
                "items": [
                    {
                        "kind": "Continuation",
                        "text": "Candidate topic continuation.",
                        "citations": citations,
                    }
                ]
            }

        one_side = [{"source_id": a.id, "quote": a.text}]
        result = cross_channel_links(
            [a, b],
            {"semantic_links": True, "window_minutes": 1},
            infer=lambda p, s: suggestion(one_side),
        )
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(result["items"][0]["label"], "Unresolved reference suggestion")
        self.assertEqual(result["metrics"]["semantic_link_candidates"], 0)
        self.assertEqual(result["metrics"]["unresolved_semantic_suggestions"], 1)
        repeated = cross_channel_links(
            [a, b],
            {"semantic_links": True, "window_minutes": 1},
            infer=lambda p, s: suggestion(one_side * 2),
        )
        self.assertEqual(repeated["outcome"], "partial")
        two_sides = one_side + [{"source_id": b.id, "quote": b.text}]
        complete = cross_channel_links(
            [a, b],
            {"semantic_links": True, "window_minutes": 1},
            infer=lambda p, s: suggestion(two_sides),
        )
        self.assertEqual(complete["outcome"], "complete")
        self.assertEqual(complete["metrics"]["semantic_link_candidates"], 1)
        self.assertEqual(set(complete["items"][0]["source_ids"]), {a.id, b.id})

    def test_temporal_candidate_limit_discloses_partial_coverage(self):
        messages = [
            self.message(f"Message {number}", number / 10) for number in range(500)
        ]
        result = cross_channel_links(messages, {})
        self.assertEqual(len(result["items"]), 250)
        self.assertEqual(result["metrics"]["omitted_temporal_candidates"], 249)
        self.assertEqual(result["outcome"], "partial")

    def test_session_summaries_preserve_chunk_and_exact_quotes(self):
        a = self.message("I proposed a meeting, but it is cancelled.")
        b = self.message(
            "Please confirm the cancellation.",
            1,
            sender=self.person_b,
            recipient=self.person_a,
        )

        def infer(prompt, schema):
            self.assertIn("inferred session", prompt)
            return {
                "items": [
                    {
                        "kind": "summary",
                        "text": "A proposed meeting is cancelled.",
                        "citations": [{"source_id": a.id, "quote": a.text}],
                    }
                ]
            }

        result = session_summaries([a, b], {}, infer=infer)
        self.assertEqual(result["metrics"]["summary_chunks"], 1)
        self.assertEqual(result["items"][0]["values"]["chunk_source_ids"], [a.id, b.id])
        self.assertEqual(result["items"][0]["values"]["inferred_session"], 1)
        self.assertIn("misinterpret sarcasm", " ".join(result["warnings"]))
        self.assertEqual(
            session_summaries([a], {}, infer=None)["outcome"], "unsupported"
        )
        with self.assertRaises(ValidationError):
            session_summaries([self.message("x" * 16001)], {}, infer=infer)

    def test_session_summary_aggregation_preserves_missing_chunk_content(self):
        blank = self.message(" ")
        infer = Mock(return_value={"items": []})
        result = session_summaries([blank], {}, infer=infer)
        self.assertEqual(result["outcome"], "insufficient")
        infer.assert_not_called()
        self.assertIn("Session 1: No retained content", " ".join(result["warnings"]))

        usable = self.message("A retained message.", 200)
        result = session_summaries([blank, usable], {}, infer=infer)
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(result["items"], [])
        self.assertEqual(result["metrics"]["summary_chunks"], 2)
        infer.assert_called_once()
        complete = session_summaries([usable], {}, infer=infer)
        self.assertEqual(complete["outcome"], "empty")

    def test_automatic_session_summary_sends_only_chunks_with_new_sources(self):
        earlier = self.message("Retained historical content.")
        anchor = self.message("New evidence anchors this summary.", 200)
        infer = Mock(return_value={"items": []})
        result = session_summaries(
            [earlier, anchor], {"_comparison": {"anchors": [anchor.id]}}, infer=infer
        )
        self.assertEqual(result["metrics"]["summary_chunks"], 1)
        infer.assert_called_once()
        prompt = infer.call_args.args[0]
        self.assertIn(anchor.text, prompt)
        self.assertNotIn(earlier.text, prompt)
