import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.test import SimpleTestCase

from core.analysis.errors import AnalysisError, error_message
from core.analysis.sources import Source
from text.analysis.ai import content_events
from text.analysis.attributed_writing import analyze_attributed_writing
from text.analysis.deterministic import (
    copied_text,
    extract_entities,
    keywords,
    style_changes,
    style_profile,
)
from text.analysis.workflows import text_topics


def source(text, *, kind="text", identifier=None, **values):
    item = SimpleNamespace(
        pk=uuid4(), case_id=uuid4(), body=text, language="en", **values
    )
    return Source(str(identifier or uuid4()), kind, item)


SAMPLE = (
    "Hello, we arrived at the harbour before the scheduled "
    "meeting. I will send your package tomorrow, but please"
    " confirm the address first. Our notes include several "
    "different subjects and enough original wording to "
    "describe punctuation and vocabulary. 😊 Thanks!"
)


class AttributedWritingTests(SimpleTestCase):
    def analyze(self, sources, *, period_split=None, date_only=None):
        scope = {} if period_split is None else {"period_split": period_split}
        run = SimpleNamespace(
            inputs=sources,
            operation="style_change" if scope else "style_profiles",
        )
        with (
            patch(
                "text.analysis.attributed_writing.source_batches",
                return_value=(sources,),
            ),
            patch(
                "text.analysis.attributed_writing.date_only_message_dates",
                return_value=date_only or {},
            ),
        ):
            return analyze_attributed_writing(self, run, scope, Mock())

    def test_profile_phrase_anchors_use_the_actual_punctuated_words(self):
        unrelated = source("Entirely separate message.", kind="text-message")
        repeated = source("One, two three. One, two three.", kind="text-message")
        result = self.analyze([unrelated, repeated])
        item = next(
            item
            for item in result["items"]
            if item["label"] == "Repeated three-word phrases"
        )
        self.assertEqual(item["values"]["one two three"], 2)
        self.assertEqual(item["source_ids"], [repeated.id])
        self.assertEqual(
            [repeated.text[span["start"] : span["end"]] for span in item["anchors"]],
            ["One, two three", "One, two three"],
        )

    def test_comparison_retains_original_for_wholly_excluded_examples(self):
        earlier = source(SAMPLE, kind="text-message", occurred_at=None)
        excluded = source(
            '"This passage is quoted."', kind="text-message", occurred_at=None
        )
        later = source(SAMPLE.upper(), kind="text-message", occurred_at=None)
        result = self.analyze([earlier, excluded, later], period_split=2)
        item = next(
            item for item in result["items"] if item["label"] == "Group A coverage"
        )
        example = item["values"]["excluded_span_examples"][0]
        self.assertEqual(example["source_id"], excluded.id)
        self.assertIn(excluded.id, item["source_ids"])
        self.assertIn(excluded.anchor(start=0, end=len(excluded.text)), item["anchors"])

    def test_quote_dense_original_does_not_stop_the_rest_of_the_selection(self):
        quotes = source(
            '"Quoted wording." ' * 201 + "Uncounted words.", kind="text-message"
        )
        usable = source(SAMPLE, kind="text-message")
        result = self.analyze([quotes, usable])
        self.assertEqual(result["outcome"], "complete")
        self.assertEqual(result["metrics"]["sources_selected"], 2)
        self.assertEqual(result["metrics"]["sources_usable"], 1)
        excluded = [
            item for item in result["items"] if item["label"] == "Excluded sample span"
        ]
        self.assertEqual(len(excluded), 1)
        self.assertEqual(
            excluded[0]["anchors"], [quotes.anchor(start=0, end=len(quotes.text))]
        )
        self.assertIn("too many quotation spans", excluded[0]["text"])
        insufficient = self.analyze([quotes])
        self.assertEqual(insufficient["outcome"], "insufficient")
        self.assertIn("too many quotation spans", insufficient["items"][0]["text"])
        legacy = style_profile([quotes], {})
        self.assertEqual(legacy["outcome"], "insufficient")
        self.assertIn(
            "A quotation-heavy original was excluded in full.", legacy["warnings"]
        )

    def test_date_only_placeholder_is_not_a_reported_comparison_time(self):
        earlier = source(
            SAMPLE.lower(),
            kind="text-message",
            occurred_at=datetime(2026, 10, 1, 12, tzinfo=UTC),
        )
        later = source(
            SAMPLE.upper(),
            kind="text-message",
            occurred_at=datetime(2026, 10, 5, tzinfo=UTC),
        )
        sources = [earlier, later]
        exact_times = self.analyze(sources, period_split=1)
        self.assertIsNotNone(exact_times["metrics"]["possible_change_interval"])

        date_only = {later.item.pk: later.item.occurred_at.date()}
        result = self.analyze(sources, period_split=1, date_only=date_only)
        item = next(
            item for item in result["items"] if item["label"] == "Group B coverage"
        )
        self.assertEqual(item["values"]["sources_with_reported_event_time"], 0)
        self.assertEqual(item["values"]["sources_missing_reported_event_time"], 1)
        self.assertIsNone(result["metrics"]["possible_change_interval"])


class TextAnalysisTests(SimpleTestCase):
    def test_entities_preserve_raw_strings_and_exact_spans(self):
        text = (
            "Email My.Name@EXAMPLE.test and visit "
            "https://example.test/Path. Call +44 20 7946 0123 on "
            "03/04/2026 or 2026-10-27. The amount was $120.50; "
            "reference CASE-1234. Coordinates 45.2, -73.4 remain a "
            "candidate."
        )
        original = source(text)
        result = extract_entities([original], {})
        labels = {item["label"] for item in result["items"]}
        self.assertTrue(
            {
                "Email",
                "URL",
                "Phone candidate",
                "Date candidate",
                "Amount candidate",
                "Identifier candidate",
                "Coordinate candidate",
            }
            <= labels
        )
        for item in result["items"]:
            anchor = item["anchors"][0]
            self.assertEqual(text[anchor["start"] : anchor["end"]], item["text"])
        email = next(i for i in result["items"] if i["label"] == "Email")
        self.assertEqual(email["values"]["candidate"], "My.Name@example.test")
        url = next(i for i in result["items"] if i["label"] == "URL")
        self.assertEqual(url["text"], "https://example.test/Path")
        self.assertEqual(original.text, text)

    def test_email_sentence_punctuation_does_not_hide_occurrence(self):
        original = source("Write to source@example.test.")
        result = extract_entities([original], {})
        email = next(item for item in result["items"] if item["label"] == "Email")
        self.assertEqual(email["text"], "source@example.test")
        self.assertEqual(email["anchors"][0]["end"], len(original.text) - 1)

    def test_fully_excluded_original_returns_no_unanchored_profile_items(self):
        original = source(SAMPLE)
        result = style_profile(
            [original], {"exclusions": {original.id: [[0, len(SAMPLE)]]}}
        )
        self.assertEqual(result["outcome"], "insufficient")
        self.assertEqual(result["items"], [])
        self.assertEqual(result["metrics"]["words"], 0)

    def test_entities_empty_and_ambiguous_values(self):
        result = extract_entities([source("Nothing explicit here.")], {})
        self.assertEqual(result["outcome"], "empty")
        result = extract_entities(
            [source("Date 04/03/26, CAD 20, and coordinate 91.0, 180.0")], {}
        )
        self.assertFalse(
            any(x["label"] == "Coordinate candidate" for x in result["items"])
        )
        self.assertIn(
            "not inferred",
            next(x for x in result["items"] if x["label"] == "Date candidate")[
                "values"
            ]["interpretation"],
        )

    def test_bare_domains_have_exact_spans_and_conservative_boundaries(self):
        original = source("Use WWW.Example.COM and sub.example.co.uk; example.test.")
        result = extract_entities([original], {})
        domains = [
            item for item in result["items"] if item["label"] == "Domain candidate"
        ]
        self.assertEqual(
            [item["text"] for item in domains],
            ["WWW.Example.COM", "sub.example.co.uk", "example.test"],
        )
        self.assertEqual(domains[0]["values"]["candidate"], "www.example.com")
        for item in domains:
            anchor = item["anchors"][0]
            self.assertEqual(
                original.text[anchor["start"] : anchor["end"]], item["text"]
            )
        misleading = source(
            (
                "document.pdf release.version 3.1415 foo..com -bad.com "
                "bad-.com éxample.com "
            )
            + "a" * 64
            + ".com "
            + "https://url.example.com user@mail.example.com"
        )
        self.assertFalse(
            any(
                item["label"] == "Domain candidate"
                for item in extract_entities([misleading], {})["items"]
            )
        )

    def test_profile_adequacy_and_observed_features(self):
        original = source(SAMPLE)
        result = style_profile([original], {})
        self.assertEqual(result["outcome"], "complete")
        self.assertGreaterEqual(result["metrics"]["words"], 30)
        self.assertTrue(any(x["label"] == "Greeting" for x in result["items"]))
        self.assertTrue(any(x["label"] == "Sign-off" for x in result["items"]))
        self.assertIn("do not establish authorship", " ".join(result["warnings"]))
        for item in result["items"]:
            for anchor in item["anchors"]:
                self.assertTrue(0 <= anchor["start"] <= anchor["end"] <= len(SAMPLE))
        self.assertEqual(original.text, SAMPLE)

    def test_tiny_and_repetitive_profiles_are_insufficient(self):
        for text in ("hi", "hello " * 100):
            self.assertEqual(
                style_profile([source(text)], {})["outcome"], "insufficient"
            )

    def test_exclusions_and_quotation_spans_preserve_offsets(self):
        original = source(
            '"Quoted words do not describe the selected writer." ' + SAMPLE
        )
        end = original.text.index("Hello")
        result = style_profile(
            [original], {"exclusions": {original.id: [[end, end + 5]]}}
        )
        self.assertEqual(result["metrics"]["excluded_spans"], 2)
        self.assertTrue(
            any(x["label"] == "Excluded sample span" for x in result["items"])
        )
        with self.assertRaises(ValidationError):
            style_profile([original], {"exclusions": {original.id: [[-1, 3]]}})
        with self.assertRaises(ValidationError):
            style_profile([original], {"exclusions": {"unselected": [[0, 3]]}})

    def test_exact_phrase_exclusions_preserve_all_occurrences(self):
        original = source("copied phrase " + SAMPLE + " copied phrase")
        result = style_profile([original], {"exclude_phrases": "copied phrase"})
        self.assertEqual(result["metrics"]["excluded_spans"], 2)
        self.assertEqual(original.text.count("copied phrase"), 2)
        repeated = source("word " * 201)
        with self.assertRaises(ValidationError):
            style_profile([repeated], {"exclude_phrases": "word"})

    def test_derived_and_translated_samples_not_writer_formatting(self):
        original = source(SAMPLE)
        call = source(SAMPLE, kind="phone-call")
        result = style_profile(
            [original, call],
            {"contexts": {original.id: {"origin": "translation", "language": "fr"}}},
        )
        self.assertEqual(result["outcome"], "insufficient")
        self.assertEqual(result["metrics"]["sources_usable"], 0)
        self.assertEqual(result["items"], [])

    def test_style_comparison_retains_groups_and_exclusions(self):
        a = source(SAMPLE)
        b = source(SAMPLE.upper())
        result = style_changes(
            [a, b],
            {"group_a": [a.id], "group_b": [b.id], "exclusions": {a.id: [[0, 5]]}},
        )
        self.assertEqual(result["outcome"], "complete")
        capitalization = next(
            x for x in result["items"] if x["label"] == "Uppercase letter ratio"
        )
        self.assertGreater(capitalization["values"]["difference_b_minus_a"], 0)
        with self.assertRaises(ValidationError):
            style_changes([a, b], {"group_a": [a.id], "group_b": [a.id]})
        result = style_changes(
            [a, source("hi", identifier="small")],
            {"group_a": [a.id], "group_b": ["small"]},
        )
        self.assertEqual(result["outcome"], "insufficient")
        self.assertTrue(
            all(
                x["values"].get("difference_b_minus_a") is None
                for x in result["items"][:5]
            )
        )

    def test_profile_examples_do_not_include_masked_openings_or_fake_phrases(self):
        prefix = "EXCLUDED OPENING. "
        original = source(prefix + SAMPLE)
        result = style_profile(
            [original], {"exclusions": {original.id: [[0, len(prefix)]]}}
        )
        for item in result["items"]:
            if item["label"] != "Excluded sample span":
                self.assertTrue(
                    all(anchor["start"] >= len(prefix) for anchor in item["anchors"])
                )
        punctured = source("oneXtwo three " + SAMPLE)
        actual = source("one two three one two three")
        result = style_profile(
            [punctured, actual], {"exclusions": {punctured.id: [[3, 4]]}}
        )
        feature = next(
            item
            for item in result["items"]
            if item["label"] == "Repeated three-word phrases"
        )
        self.assertEqual(feature["values"]["one two three"], 2)
        self.assertTrue(
            all(anchor["source_id"] == actual.id for anchor in feature["anchors"])
        )

    def test_style_change_examples_never_include_excluded_openings(self):
        prefix = "EXCLUDED OPENING THAT IS NOT WRITER EVIDENCE. "
        a = source(prefix + SAMPLE.lower())
        b = source(prefix + SAMPLE.upper())
        selected = {a.id: a, b.id: b}
        result = style_changes(
            [a, b],
            {
                "group_a": [a.id],
                "group_b": [b.id],
                "exclusions": {a.id: [[0, len(prefix)]], b.id: [[0, len(prefix)]]},
            },
        )
        capitalization = next(
            item
            for item in result["items"]
            if item["label"] == "Uppercase letter ratio"
        )
        examples = capitalization["values"]["supporting_examples"]
        self.assertEqual({item["group"] for item in examples}, {"A", "B"})
        for item in result["items"]:
            for anchor in item["anchors"]:
                self.assertGreaterEqual(anchor["start"], len(prefix))
                self.assertLessEqual(
                    anchor["end"], len(selected[anchor["source_id"]].text)
                )
        self.assertTrue(
            any(
                item["label"] == "Recurring wording comparison"
                for item in result["items"]
            )
        )
        self.assertIsNone(result["metrics"]["possible_change_interval"])

    def test_style_change_retains_observed_contrary_samples(self):
        a_low, a_high = source(SAMPLE.lower()), source(SAMPLE.upper())
        b_mixed = source(SAMPLE.upper() + " " + SAMPLE.lower())
        b_high = source(SAMPLE.upper())
        result = style_changes(
            [a_low, a_high, b_mixed, b_high],
            {
                "group_a": [a_low.id, a_high.id],
                "group_b": [b_mixed.id, b_high.id],
            },
        )
        feature = next(
            item
            for item in result["items"]
            if item["label"] == "Uppercase letter ratio"
        )
        self.assertTrue(
            any(
                example["source_id"] == a_high.id
                for example in feature["values"]["contrary_examples"]
            )
        )
        self.assertIn("not calibrated", " ".join(result["warnings"]))

    def test_repeated_phrases_do_not_cross_excluded_material(self):
        a = source("alpha beta OMIT gamma delta. " * 2 + SAMPLE)
        b = source(SAMPLE)
        result = style_changes(
            [a, b],
            {
                "group_a": [a.id],
                "group_b": [b.id],
                "exclude_phrases": "OMIT",
            },
        )
        phrases = [
            item["text"]
            for item in result["items"]
            if item["label"] == "Recurring wording comparison"
        ]
        self.assertNotIn("alpha beta gamma", phrases)
        profile = style_profile([a], {"exclude_phrases": "OMIT"})
        observed = next(
            item
            for item in profile["items"]
            if item["label"] == "Repeated three-word phrases"
        )
        self.assertNotIn("alpha beta gamma", observed["values"])

    def test_style_time_coverage_and_guarded_possible_interval(self):
        first = datetime(2026, 10, 1, 12, tzinfo=UTC)
        a = source(SAMPLE.lower(), kind="text-message", occurred_at=first)
        b = source(
            SAMPLE.upper(), kind="text-message", occurred_at=first + timedelta(days=5)
        )
        parameters = {"group_a": [a.id], "group_b": [b.id]}
        result = style_changes([a, b], parameters)
        self.assertEqual(
            result["metrics"]["possible_change_interval"]["start"], first.isoformat()
        )
        self.assertEqual(
            result["metrics"]["possible_change_interval"]["end"],
            b.item.occurred_at.isoformat(),
        )
        coverage = next(
            item for item in result["items"] if item["label"] == "Group A coverage"
        )
        self.assertEqual(coverage["values"]["sources_with_reported_event_time"], 1)
        case = SimpleNamespace(pk=uuid4())
        with patch(
            "text.analysis.deterministic.date_only_message_dates",
            return_value={b.item.pk: b.item.occurred_at.date()},
        ) as classify:
            date_only = style_changes([a, b], parameters, case=case)
        classify.assert_called_once_with(case, [a.item.pk, b.item.pk])
        self.assertIsNone(date_only["metrics"]["possible_change_interval"])
        later = next(
            item for item in date_only["items"] if item["label"] == "Group B coverage"
        )
        self.assertEqual(later["values"]["sources_missing_reported_event_time"], 1)
        b.item.occurred_at = None
        self.assertIsNone(
            style_changes([a, b], parameters)["metrics"]["possible_change_interval"]
        )
        b.item.occurred_at = first
        self.assertIsNone(
            style_changes([a, b], parameters)["metrics"]["possible_change_interval"]
        )

    def test_copied_text_handles_exact_light_edit_and_unrelated(self):
        passage = (
            "The synthetic harbour delivery will arrive after the "
            "final evening inspection."
        )
        a = source(passage)
        b = source(passage.replace("delivery", "shipment"))
        exact = source(passage.upper())
        unrelated = source(
            "Completely different discussion about growing trees "
            "and testing scientific ideas tomorrow."
        )
        result = copied_text([a, b, exact, unrelated], {})
        self.assertEqual(result["outcome"], "complete")
        self.assertTrue(
            any(set(x["source_ids"]) == {a.id, b.id} for x in result["items"])
        )
        self.assertTrue(
            any(set(x["source_ids"]) == {a.id, exact.id} for x in result["items"])
        )
        self.assertFalse(any(unrelated.id in x["source_ids"] for x in result["items"]))
        for item in result["items"]:
            self.assertEqual(len(item["anchors"]), 2)
            self.assertIn("exclusion_candidate", item["values"])
        self.assertEqual(
            copied_text([source("Hi there"), source("Hi there")], {})["outcome"],
            "empty",
        )

    def test_passage_word_limit_is_checked_before_comparing_any_pairs(self):
        oversized = source("word " * 5001)
        for selections in ([oversized], [source("brief text"), oversized]):
            with (
                self.subTest(sources=len(selections)),
                self.assertRaisesMessage(ValidationError, "no more than 5,000 words"),
            ):
                copied_text(selections, {})

    def test_keywords_do_not_require_inference(self):
        original = source("Harbour harbour delivery. The package needs delivery.")
        with patch("text.analysis.ai.cited_model_analysis") as inference:
            result = keywords([original], {})
            observed_topics = text_topics([original], {"include_summary": False})
        inference.assert_not_called()
        self.assertEqual(result["items"][0]["values"]["occurrences"], 2)
        self.assertEqual(observed_topics, result)
        without_runtime = text_topics([original], {})
        self.assertEqual(without_runtime["outcome"], "partial")
        self.assertEqual(without_runtime["items"], result["items"])
        self.assertIn(
            "approved Cloudflare AI Gateway", " ".join(without_runtime["warnings"])
        )

    def test_topics_with_empty_model_output_keep_keywords_and_cautions(self):
        original = source("Harbour harbour delivery.")
        infer = Mock(return_value={"items": []})
        result = text_topics([original], {}, infer=infer)
        infer.assert_called_once()
        self.assertEqual(result["outcome"], "complete")
        self.assertEqual(result["items"], keywords([original], {})["items"])
        warnings = " ".join(result["warnings"])
        self.assertIn("English stop-word", warnings)
        self.assertIn("misinterpret sarcasm", warnings)
        empty = text_topics([source(" ")], {}, infer=None)
        self.assertEqual(empty["outcome"], "insufficient")
        self.assertEqual(empty["items"], [])

    def test_entirely_rejected_topics_retain_keywords(self):
        original = source("Harbour harbour delivery.")
        invalid = {
            "kind": "Topic",
            "text": "Unverified suggestion",
            "citations": [{"source_id": original.id, "quote": "Absent quotation"}],
        }
        for response in ({"items": [invalid]}, {"items": "invalid"}):
            with self.subTest(response=response):
                result = text_topics([original], {}, infer=Mock(return_value=response))
                self.assertEqual(result["outcome"], "partial")
                self.assertIn("topic suggestions were not published", result["summary"])
                self.assertEqual(result["items"], keywords([original], {})["items"])
                self.assertIn("Topic suggestions", " ".join(result["warnings"]))
                self.assertNotIn("Absent quotation", str(result))
                self.assertNotIn("Unverified suggestion", str(result))

    def test_topic_validation_warning_never_contains_exception_text(self):
        original = source("Harbour delivery.")
        result = text_topics(
            [original],
            {},
            infer=Mock(side_effect=ValidationError("private provider diagnostic")),
        )
        self.assertEqual(result["outcome"], "partial")
        self.assertNotIn("private provider diagnostic", str(result))

    def test_topic_input_limit_retains_bounded_keywords_without_inference(self):
        original = source("harbour " * 16001)
        infer = Mock()
        result = text_topics([original], {}, infer=infer)
        infer.assert_not_called()
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(result["items"], keywords([original], {})["items"])
        self.assertIn(error_message("model_content_limit"), result["warnings"])
        with self.assertRaises(ValidationError):
            text_topics([source("harbour " * 125001)], {}, infer=infer)

    def test_topic_service_failures_keep_keywords_and_safe_recovery_guidance(self):
        original = source("Harbour delivery.")
        for code in (
            "gateway_unavailable",
            "gateway_invalid_response",
        ):
            with self.subTest(code=code):
                result = text_topics(
                    [original], {}, infer=Mock(side_effect=AnalysisError(code))
                )
                self.assertEqual(result["outcome"], "partial")
                self.assertEqual(result["items"], keywords([original], {})["items"])
                self.assertIn(error_message(code), " ".join(result["warnings"]))

    def test_topics_do_not_recover_source_access_identity_or_unexpected_failures(self):
        original = source("Harbour delivery.")
        for error in (
            AnalysisError("access_removed"),
            AnalysisError("source_changed"),
            AnalysisError("source_unavailable"),
            AnalysisError("execution_changed"),
            PermissionDenied(),
            Http404(),
            RuntimeError(),
        ):
            with self.subTest(error=type(error), code=str(error)):
                with self.assertRaises(type(error)):
                    text_topics([original], {}, infer=Mock(side_effect=error))
        with self.assertRaises(AnalysisError):
            text_topics(
                [source("a")],
                {},
                infer=Mock(side_effect=AnalysisError("gateway_unavailable")),
            )

    def test_model_suggestions_require_exact_quotes_and_selected_sources(self):
        original = source("I will meet you tomorrow. Actually, cancel that proposal.")

        def infer(prompt, schema):
            self.assertIn("untrusted evidence", prompt)
            self.assertIn("negation", prompt)
            self.assertEqual(schema["type"], "object")
            return {
                "items": [
                    {
                        "kind": "Cancelled proposal",
                        "text": "The source cancels an earlier proposed meeting.",
                        "status": "cancelled",
                        "citations": [
                            {"source_id": original.id, "quote": original.text}
                        ],
                    }
                ]
            }

        result = content_events([original], {}, infer=infer)
        self.assertEqual(result["items"][0]["values"]["status"], "cancelled")
        for citation in (
            {"source_id": "other", "quote": "I will"},
            {"source_id": original.id, "quote": "I never said this"},
            {"source_id": original.id, "quote": "I will", "start": True},
        ):
            with self.subTest(citation=citation), self.assertRaises(ValidationError):
                content_events(
                    [original],
                    {},
                    infer=lambda p, s, c=citation: {
                        "items": [
                            {"kind": "claim", "text": "Candidate", "citations": [c]}
                        ]
                    },
                )

    def test_message_model_context_keeps_ids_and_source_text(self):
        sender_id, recipient_id = uuid4(), uuid4()
        original = source(
            "Exact message wording.",
            kind="text-message",
            sender_id=sender_id,
            recipient_id=recipient_id,
            sender="Alex Example",
            recipient="Pat Example",
            occurred_at=None,
        )

        def infer(prompt, schema):
            context = json.loads(prompt.split("Evidence:\n", 1)[1])[0]
            self.assertNotIn("Alex Example", prompt)
            self.assertNotIn("Pat Example", prompt)
            self.assertEqual(context["reported_sender_id"], str(sender_id))
            self.assertEqual(context["reported_recipient_id"], str(recipient_id))
            self.assertEqual(context["text"], original.text)
            self.assertIn("reported sender <reported_sender_id>", prompt)
            return {"items": []}

        self.assertEqual(
            content_events([original], {}, infer=infer)["outcome"], "empty"
        )

    def test_model_contract_discloses_citation_bounds_and_exact_source_ids(self):
        original = source("A delivery was cancelled.")
        other = source("An unrelated source.")

        def infer(prompt, schema):
            suggestions = schema["properties"]["items"]
            candidate = suggestions["items"]["properties"]
            citations = candidate["citations"]
            citation = citations["items"]["properties"]
            self.assertEqual(suggestions["maxItems"], 100)
            self.assertEqual(candidate["text"]["minLength"], 1)
            self.assertEqual(candidate["text"]["maxLength"], 4000)
            self.assertEqual(citations["minItems"], 1)
            self.assertEqual(citations["maxItems"], 10)
            self.assertEqual(citation["quote"]["minLength"], 1)
            self.assertEqual(citation["quote"]["maxLength"], 4000)
            self.assertEqual(
                set(citation["source_id"]["enum"]), {original.id, other.id}
            )
            self.assertIn("contiguous substring", prompt)
            self.assertIn("Omit start", prompt)
            return {"items": []}

        self.assertEqual(
            content_events([original, other], {}, infer=infer)["outcome"], "empty"
        )

    def test_unique_unicode_quote_is_anchored_without_a_correct_model_offset(self):
        original = source("🙂 The gate’s closed.\nTry tomorrow.")
        quote = "The gate’s closed."
        for offset in (None, 0, 3):
            with self.subTest(offset=offset):
                result = content_events(
                    [original],
                    {},
                    infer=lambda prompt, schema, offset=offset: {
                        "items": [
                            {
                                "kind": "cancellation",
                                "text": "The reported sender says the gate is closed.",
                                "citations": [
                                    {
                                        "source_id": original.id,
                                        "quote": quote,
                                        "start": offset,
                                    }
                                ],
                            }
                        ]
                    },
                )
                self.assertEqual(
                    result["items"][0]["anchors"],
                    [original.anchor(start=2, end=2 + len(quote))],
                )

    def test_topics_and_claims_allow_up_to_128000_selected_characters(self):
        for analyze in (text_topics, content_events):
            with self.subTest(analyze=analyze.__name__):
                exact = source("x" * 128_000)
                parameters = {"_suggestions_only": True}
                infer = Mock(return_value={"items": []})
                result = analyze([exact], parameters, infer=infer)
                self.assertEqual(result["metrics"]["characters"], 128_000)
                self.assertEqual(result["metrics"]["method"], "cited-content-v2")
                self.assertIn(exact.text, infer.call_args.args[0])
                infer.reset_mock()
                with self.assertRaises(ValidationError) as caught:
                    analyze([source(exact.text + "x")], parameters, infer=infer)
                self.assertEqual(caught.exception.code, "model_content_limit")
                infer.assert_not_called()

    def test_model_validation_has_safe_distinct_diagnostic_categories(self):
        original = source("The same reply. The same reply.")
        valid = {
            "kind": "claim",
            "text": "A source-cited suggestion.",
            "citations": [{"source_id": original.id, "quote": original.text}],
        }
        cases = (
            ({"items": {}}, "model_result_structure"),
            ({"items": [valid | {"citations": []}]}, "model_suggestion_citations"),
            ({"items": [valid | {"citations": [None]}]}, "model_citation_structure"),
            (
                {
                    "items": [
                        valid | {"citations": [{"source_id": "other", "quote": "The"}]}
                    ]
                },
                "model_citation_source",
            ),
            (
                {
                    "items": [
                        valid
                        | {"citations": [{"source_id": original.id, "quote": "reply"}]}
                    ]
                },
                "model_quote_offset_required",
            ),
            (
                {
                    "items": [
                        valid
                        | {"citations": [{"source_id": original.id, "quote": "Absent"}]}
                    ]
                },
                "model_quote_mismatch",
            ),
            (
                {
                    "items": [
                        valid
                        | {
                            "citations": [
                                {"source_id": original.id, "quote": "reply", "start": 0}
                            ]
                        }
                    ]
                },
                "model_quote_mismatch",
            ),
        )
        for response, code in cases:
            with self.subTest(code=code), self.assertRaises(ValidationError) as caught:
                content_events([original], {}, infer=lambda p, s, value=response: value)
            self.assertEqual(caught.exception.code, code)

    def test_model_repeated_quote_needs_offset_and_missing_content_is_explicit(self):
        original = source("yes yes aaa")
        suggestion = {
            "kind": "claim",
            "text": "Candidate",
            "citations": [{"source_id": original.id, "quote": "yes"}],
        }
        with self.assertRaises(ValidationError):
            content_events([original], {}, infer=lambda p, s: {"items": [suggestion]})
        overlapping = suggestion | {
            "citations": [{"source_id": original.id, "quote": "aa"}]
        }
        with self.assertRaises(ValidationError) as caught:
            content_events([original], {}, infer=lambda p, s: {"items": [overlapping]})
        self.assertEqual(caught.exception.code, "model_quote_offset_required")
        suggestion["citations"][0]["start"] = 4
        self.assertEqual(
            content_events([original], {}, infer=lambda p, s: {"items": [suggestion]})[
                "outcome"
            ],
            "complete",
        )
        self.assertEqual(
            content_events([source("")], {}, infer=None)["outcome"], "insufficient"
        )
        self.assertEqual(
            content_events([original], {}, infer=None)["outcome"], "unsupported"
        )

    def test_selected_text_resource_bounds(self):
        with self.assertRaises(ValidationError):
            keywords([source("word" * 250001)], {})
        with self.assertRaises(ValidationError):
            copied_text([source("a") for _ in range(51)], {})
