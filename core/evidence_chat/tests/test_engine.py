"""End-to-end worker checks with synthetic records and mocked model calls."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import Value
from django.test import SimpleTestCase, TestCase, override_settings

from cases.models import CaseMembership
from core.analysis.approval import revoke_case_execution
from core.analysis.catalogue import project_result
from core.analysis.errors import AnalysisError
from core.analysis.execution import configured_external_executions
from core.analysis.source_resolution import resolve_inputs
from core.analysis.source_selection import snapshot_sources
from core.analysis.testing import approve_case_execution
from core.analysis.tests.test_execution import LOCAL
from core.analysis.tests.test_gateway import response
from core.analysis.tests.test_openai_gateway import OPENAI, openai_reply
from core.evidence_chat import (
    ai,
    corpus,
    engine,
    engine_counts,
    full_review,
    jobs,
    services,
    turn_retrieval,
)
from core.evidence_chat.checkpoints import authorize_turn
from core.evidence_chat.history import reply_context
from core.evidence_chat.search_scope import review_population, review_restart_code
from core.models import AnalysisResult, AnalysisRun, ExtractedPassage, GatewayRequest
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from people.models import Person
from phone.analysis.deterministic.conversations import interaction_patterns
from phone.models import PhoneCallEvidence, TextMessageEvidence
from text.models import TextEvidence


def decision(
    action, question="", *, claims=None, lead="", missing=None, independent=False
):
    return {
        "action": action,
        "question": question,
        "missing_fact": missing
        if missing is not None
        else (f"An original recording {question}." if action == "refine" else ""),
        "independent_source": independent,
        "claims": claims or [],
        "lead": lead,
        "clarification": "",
    }


class QuestionRoutingTests(SimpleTestCase):
    def test_explicit_review_population_ignores_quoted_wording(self):
        for question, expected in (
            ("Please inspect every message for delays.", "text-message"),
            (
                "After searching all the text messages, explain the delay.",
                "text-message",
            ),
            ("Do a full review of the messages for delays.", "text-message"),
            ("Review every record that mentions messages.", "all"),
            ("Who said ‘inspect every message’?", ""),
        ):
            with self.subTest(question=question):
                self.assertEqual(review_population(question), expected)

    def test_answer_context_identifies_repeated_material_from_one_original(self):
        turn = SimpleNamespace(
            question="Was the dispatch corroborated?",
            plan={},
            execution={
                "backend": "gateway",
                "max_request_bytes": 32000,
            },
        )
        original = {"record_key": "text:1", "evidence_id": "1", "label": "Dispatch"}
        derived = {
            "evidence_id": None,
            "cited_originals": ["text:1"],
            "label": "Finding",
        }
        independent = {"record_key": "text:2", "evidence_id": "2", "label": "Witness"}
        windows = [
            engine.Window(loc, 0, "Synthetic account.")
            for loc in (original, derived, independent)
        ]
        with patch.object(ai, "infer", return_value=decision("answer")) as model:
            engine.decide(turn, None, [], windows)
        records = json.loads(model.call_args.args[2].split("\n", 1)[1])["records"]
        self.assertEqual(records[1]["shares_original_with"], [0])
        self.assertNotIn("shares_original_with", records[2])

    def test_refinement_checks_unread_content_and_original_independence(self):
        def window(key, original, start, text, *, strong=False, surface="original"):
            return engine.Window(
                {
                    "key": key,
                    "surface": surface,
                    "label": "Ledger",
                    "record_key": original,
                    "evidence_id": original,
                },
                start,
                text,
                semantic_match=strong,
            )

        prior = [window("page1", "book1", 0, "The kestrel was recorded at dawn.")]
        target = {
            "query": "kestrel",
            "missing_fact": "A second flight observation.",
            "independent_source": False,
            "answer": {"claims": []},
        }
        shifted = window(
            "page1", "book1", 4, "kestrel was recorded at dawn. Extra.", strong=True
        )
        distant = window("page1", "book1", 2100, "The second kestrel arrived at dusk.")
        copy = window(
            "summary",
            "book1",
            0,
            "A derived note reports the kestrel.",
            surface="finding",
        )
        semantic = window(
            "book2", "book2", 0, "I watched a falcon settle.", strong=True
        )
        irrelevant = window("book3", "book3", 0, "The bridge opened.")
        self.assertFalse(
            turn_retrieval.advances_lookup(target, [shifted, copy, irrelevant], prior)
        )
        self.assertTrue(turn_retrieval.advances_lookup(target, [distant], prior))
        finding_search = target | {"lookup": "search_findings"}
        self.assertTrue(turn_retrieval.advances_lookup(finding_search, [copy], prior))
        repeat = window("summary", "book1", 0, prior[0].text, surface="finding")
        self.assertFalse(
            turn_retrieval.advances_lookup(finding_search, [repeat], prior)
        )
        self.assertFalse(
            turn_retrieval.advances_lookup(finding_search, [copy], [*prior, copy])
        )
        self.assertFalse(
            turn_retrieval.advances_lookup(
                finding_search | {"independent_source": True}, [copy], prior
            )
        )
        target["independent_source"] = True
        self.assertFalse(turn_retrieval.advances_lookup(target, [distant], prior))
        self.assertTrue(turn_retrieval.advances_lookup(target, [semantic], prior))

    def test_only_a_request_to_inspect_the_full_set_overrides_an_ordinary_plan(self):
        for original, expected in (
            ("What total is printed on the receipt?", "answer"),
            ("Did the witness say “count me in”?", "answer"),
            ("Did the witness say “list every mention”?", "answer"),
            ("Give an overview of the pickup across the case.", "answer"),
            ("Find all you can about the pickup.", "answer"),
            ("Find all recipients of that statement.", "answer"),
            ("Identify every date that changed.", "answer"),
            ("How many delays were reported across the case?", "answer"),
            ("List every mention of pickup", "answer"),
            ("Check every record for a pickup", "review"),
            ("Show me every match", "answer"),
        ):
            with self.subTest(original=original):
                turn = SimpleNamespace(question=original)
                rewrite = decision("answer", "List every mention")
                with patch.object(
                    engine, "_request", return_value=(rewrite, [])
                ) as request:
                    action, _, _ = engine.decide(turn, None, [], [])
                self.assertEqual(action, expected)
                self.assertIs(
                    request.call_args.args[4],
                    engine.REVIEW_DECISION if expected == "review" else engine.DECISION,
                )

    def test_clarification_can_resume_an_exhaustive_request(self):
        history = [
            {
                "question": "Check every record for the pickup.",
                "reply_context": "Which pickup?",
            }
        ]
        reply = decision("review", "List every north gate pickup.")
        with patch.object(engine, "_request", return_value=(reply, [])) as request:
            action, _, _ = engine.decide(
                SimpleNamespace(question="The north gate."),
                None,
                history,
                [],
                clarified_review=True,
            )
        self.assertEqual(action, "review")
        self.assertEqual(request.call_args.args[3]["history"], history)
        self.assertIs(request.call_args.args[4], engine.REVIEW_DECISION)

    def test_literal_review_requires_an_unqualified_explicit_target(self):
        for question, literal in (
            ("List every active record that mentions Mica by name.", "Mica"),
            ("Find all records containing “blue crab”.", "blue crab"),
            ("Show every record mentioning 'gate C'?", "gate C"),
            ("List every record that mentions Mica.", ""),
            ("List every record that mentions Mica by name after Friday.", ""),
            ("List every record that does not mention Mica by name.", ""),
            (
                "What does 'list every active record that mentions Mica by name' mean?",
                "",
            ),
        ):
            with self.subTest(question=question):
                turn = SimpleNamespace(question=question, include_archived=False)
                self.assertEqual(engine_counts.literal_question(turn), literal)


@override_settings(PANDECT_LOCAL_AI=LOCAL)
class ChatWorkerTests(EvidenceViewTestMixin, TestCase):
    def ask(self, question, **kwargs):
        return services.submit_turn(
            self.users["editor"],
            self.case.pk,
            question,
            submission_id=uuid4(),
            **kwargs,
        )

    def process(self, turn):
        jobs.process_turn(str(turn.pk), str(turn.delivery_token))
        turn.refresh_from_db()

    def finish(self, turn):
        for _ in range(32):
            if turn.status not in ("queued", "running"):
                break
            self.process(turn)
        self.assertEqual(turn.status, "complete", turn.error_code)

    def source(self, body, title="Pickup record"):
        return TextEvidence.objects.create(
            case=self.case, uploaded_by=self.users["owner"], title=title, body=body
        )

    def test_question_clarification_then_cited_conflict_and_current_followup(self):
        self.source("North gate: cancelled. Meet at the south gate.")
        self.source("Keep the north gate pickup.")
        first = self.ask("Which pickup was confirmed?")
        with patch.object(
            ai,
            "infer",
            return_value=decision("ask", "Which gate pickup?"),
        ):
            self.process(first)
        self.assertEqual(first.mode, "clarification")
        second = self.ask("The north gate", chat_id=first.chat_id)

        def infer(turn, authorize, prompt, schema):
            authorize()
            payload = json.loads(prompt.split("\n", 1)[1])
            self.assertIs(schema, engine.DECISION)
            self.assertEqual(
                payload["history"][-1]["reply_context"], "Which gate pickup?"
            )
            claims = []
            for row in payload["records"]:
                quote = next(
                    (
                        q
                        for q in (
                            "North gate: cancelled.",
                            "Keep the north gate pickup.",
                        )
                        if q in row["text"]
                    ),
                    None,
                )
                if quote:
                    claims.append(
                        {
                            "text": quote,
                            "citations": [
                                {
                                    "id": row["id"],
                                    "quote": quote,
                                    "start": row["text"].index(quote),
                                }
                            ],
                        }
                    )
            return decision(
                "answer", claims=claims, lead="Check the time of each account."
            )

        with patch.object(ai, "infer", side_effect=infer) as model:
            self.process(second)
        model.assert_called_once()
        self.assertEqual(second.plan["timing"]["model_calls"], 1)
        self.assertEqual(second.plan["timing"]["retrieval_calls"], 1)
        self.assertEqual(second.status, "complete", second.error_code)
        answer = services.inspect_turn(second)
        self.assertEqual(len(answer["claims"]), 2)
        self.assertTrue(all(c["citations"][0]["url"] for c in answer["claims"]))
        self.assertIn("time", answer["lead"])

    def test_model_cannot_expand_a_bounded_overview_into_a_full_review(self):
        import hashlib

        quote = "The pickup moved to Red Pier."
        source = self.source(quote, "Pickup dispatch")
        unrelated = "The refinery recorded a pressure reading."
        unrelated_hash = hashlib.sha256(unrelated.encode()).hexdigest()
        TextEvidence.objects.bulk_create(
            [
                TextEvidence(
                    case=self.case,
                    uploaded_by=self.users["owner"],
                    title=f"Refinery log {i}",
                    body=unrelated,
                    sha256=unrelated_hash,
                )
                for i in range(40)
            ]
        )
        turn = self.ask("Give an overview of the pickup across the case.")

        def infer(active, authorize, prompt, schema):
            authorize()
            data = json.loads(prompt.split("\n", 1)[1])
            self.assertEqual(data["question"], turn.question)
            self.assertEqual(len(data["records"]), 1)
            record = data["records"][0]
            self.assertIn(quote, record["text"])
            if schema is engine.DECISION:
                self.assertNotIn("review", schema["properties"]["action"]["enum"])
                self.assertNotIn(
                    "review_answer", schema["properties"]["action"]["enum"]
                )
                return decision("review_answer", "Review every record for the pickup.")
            self.assertIs(schema, engine.ANSWER)
            return {
                "claims": [
                    {
                        "text": quote,
                        "citations": [
                            {
                                "id": record["id"],
                                "quote": quote,
                                "start": record["text"].index(quote),
                            }
                        ],
                    }
                ],
                "clarification": "",
                "lead": "",
            }

        with (
            patch.object(ai, "infer", side_effect=infer) as model,
            patch.object(corpus, "discover", wraps=corpus.discover) as discover,
        ):
            self.finish(turn)
        self.assertEqual(model.call_count, 2)
        discover.assert_not_called()
        self.assertEqual((turn.mode, turn.total, turn.reviewed), ("ordinary", 1, 1))
        self.assertEqual(turn.sources.get().evidence_id, source.pk)
        citation = services.inspect_turn(turn)["claims"][0]["citations"][0]
        self.assertEqual(citation["quote"], quote)
        self.assertTrue(citation["url"])

    def test_earlier_exhaustive_clarification_does_not_authorize_a_later_question(self):
        self.source("The timetable lists departures on Fridays.")
        first = self.ask("Check every record for matches. type:image")
        with patch.object(ai, "infer", return_value=decision("ask", "Which dispatch?")):
            self.finish(first)

        second = self.ask("Please summarize the timetable.", chat_id=first.chat_id)

        def infer(active, authorize, prompt, schema):
            authorize()
            self.assertIs(schema, engine.DECISION)
            payload = json.loads(prompt.split("\n", 1)[1])
            history = payload["history"]
            if active.pk == second.pk:
                self.assertIn(
                    "The timetable lists departures", payload["records"][0]["text"]
                )
            self.assertEqual(
                [entry["question"] for entry in history],
                [first.question]
                if active.pk == second.pk
                else [first.question, second.question],
            )
            return decision("answer")

        with patch.object(ai, "infer", side_effect=infer):
            self.finish(second)
            turn = self.ask("Where did the pickup happen?", chat_id=first.chat_id)
            self.finish(turn)
        self.assertEqual(second.mode, "ordinary")
        self.assertEqual(turn.mode, "ordinary")

    def test_full_literal_review_tracks_exact_mentions_and_window_boundaries(self):
        cited = self.source(" " * 898 + "MICA went first. Mica returned.")
        self.source(" " * 896 + "Micaiah appears once.")
        self.source(" " * 779 + "xMica was a filing code.")
        turn = self.ask("List every active record that mentions Mica by name.")
        with patch.object(ai, "infer", side_effect=AssertionError("literal lookup")):
            self.finish(turn)
        self.assertEqual((turn.mode, turn.plan["review_kind"]), ("full", "literal"))
        self.assertEqual(turn.plan["matched_records"], 1)
        self.assertEqual(turn.reviewed, turn.total)
        hits = list(
            turn.hits.order_by("start").values_list(
                "source__evidence_id", "start", "quote"
            )
        )
        self.assertEqual(hits, [(cited.pk, 898, "MICA"), (cited.pk, 915, "Mica")])

    def test_literal_review_counts_body_and_values_without_counting_field_labels(self):
        self.source("The log describes a delivery.", title="Dispatch log")
        recorded_value = self.source("The log gives a time.", title="Title page")
        original_text = self.source("The title was corrected.")
        turn = self.ask("Find all records that contain ‘title’")
        with patch.object(ai, "infer", side_effect=AssertionError("literal lookup")):
            self.finish(turn)
        self.assertEqual(turn.plan["matched_records"], 2)
        hits = {
            hit.source.evidence_id: hit for hit in turn.hits.select_related("source")
        }
        self.assertEqual(set(hits), {original_text.pk, recorded_value.pk})
        self.assertEqual(
            hits[original_text.pk].start, original_text.body.index("title")
        )
        value_hit = hits[recorded_value.pk]
        self.assertEqual(value_hit.quote, "Title")
        self.assertTrue(
            corpus.cite(self.case, value_hit.source.locator, value_hit.start, "Title")
        )

    def test_filter_syntax_inside_a_literal_is_evidence_text(self):
        original = self.source("The report includes token type:image.")
        turn = self.ask("Find all records that contain ‘token type:image’")
        with patch.object(ai, "infer", side_effect=AssertionError("literal lookup")):
            self.finish(turn)
        hit = turn.hits.get()
        self.assertEqual(
            (hit.source.evidence_id, hit.quote), (original.pk, "token type:image")
        )

    def test_legacy_literal_review_cannot_publish_stale_matches(self):
        self.source("The log describes a delivery.")
        turn = self.ask("Find all records that contain ‘title’")
        self.process(turn)
        self.assertEqual(turn.plan["stage"], "review")
        turn.plan.pop("review_version")
        turn.save(update_fields=("plan",))
        self.process(turn)
        self.assertEqual(
            (turn.status, turn.error_code), ("failed", "chat_literal_review_retired")
        )
        turn.status = "complete"
        self.assertIsNone(services.inspect_turn(turn))

    def test_literal_review_batches_reads_and_keeps_bounded_record_citations(self):
        long = self.source("Mica " * 1400)
        later = self.source(" " * 6500 + "Mica returned.")
        turn = self.ask("List every record that mentions Mica by name.")
        with patch.object(ai, "infer", side_effect=AssertionError("literal lookup")):
            self.process(turn)
            with patch.object(corpus, "read_window", wraps=corpus.read_window) as read:
                self.process(turn)
            self.assertEqual(read.call_count, 2)
            self.assertEqual(read.call_args_list[0].args[3], 3240)
            self.finish(turn)
        self.assertEqual(turn.plan["matched_records"], 2)
        self.assertEqual(turn.reviewed, turn.total)
        self.assertEqual(
            list(
                turn.hits.filter(source__evidence_id=long.pk)
                .order_by("start")
                .values_list("start", flat=True)
            ),
            [0, 5, 10, 15],
        )
        self.assertEqual(turn.hits.get(source__evidence_id=later.pk).start, 6500)

    def test_review_reuses_the_frozen_digest_but_fully_verifies_before_completion(self):
        self.source("Mica " * 1400)
        turn = self.ask("List every record that mentions Mica by name.")
        with patch.object(ai, "infer", side_effect=AssertionError("literal lookup")):
            self.process(turn)
            with patch.object(corpus, "text_sha256", return_value=Value("invalid")):
                for _ in range(3):
                    self.process(turn)
                    self.assertEqual(turn.status, "running", turn.error_code)
                self.assertEqual(turn.reviewed, turn.total)
                self.process(turn)
        self.assertEqual((turn.status, turn.error_code), ("failed", "source_changed"))

    def test_full_followup_reuses_history_stamps_and_rejects_stale_prior_sources(self):
        quote = "Mica left the port."
        record = self.source(quote + " " * 8500)
        first = self.ask("Where was Mica last seen?")

        def answer(active, authorize, prompt, schema):
            authorize()
            self.assertIs(schema, engine.DECISION)
            records = json.loads(prompt.split("\n", 1)[1])["records"]
            selected = next(item for item in records if quote in item["text"])
            return decision(
                "answer",
                claims=[
                    {
                        "text": quote,
                        "citations": [
                            {
                                "id": selected["id"],
                                "start": selected["text"].index(quote),
                                "quote": quote,
                            }
                        ],
                    }
                ],
            )

        with patch.object(ai, "infer", side_effect=answer):
            self.finish(first)
        saved = first.sources.get(evidence_id=record.pk)
        self.assertIn("verified_body", saved.locator)
        turn = self.ask("Check every record for the departure.", chat_id=first.chat_id)
        with patch.object(corpus, "text_sha256", return_value=Value("invalid")):
            self.assertEqual(reply_context(turn)[0]["reply_context"], quote)
            self.assertIsNone(services.inspect_turn(first))

        # An older ordinary reply has no stamp. The active review verifies
        # and checkpoints it once without rewriting the old saved locator.
        saved.locator.pop("verified_body")
        saved.save(update_fields=("locator",))

        def review(active, authorize, prompt, schema):
            authorize()
            if schema is engine.REVIEW_DECISION:
                return decision("review", "List every mention of Mica's departure.")
            records = json.loads(prompt.split("\n", 1)[1])["records"]
            return {
                "records": [
                    {"id": row["id"], "outcome": "miss", "citations": []}
                    for row in records
                ],
            }

        with patch.object(ai, "infer", side_effect=review) as model:
            self.process(turn)
            self.assertEqual(turn.plan["stage"], "review")
            self.assertTrue(turn.plan["context_bodies"])
            self.assertTrue(turn.plan["review_question_sources"])
            self.source("An unrelated record added after the review froze.")
            self.process(turn)
            self.assertEqual(turn.status, "running", turn.error_code)
            with patch.object(corpus, "text_sha256", return_value=Value("invalid")):
                self.assertEqual(authorize_turn(turn).pk, self.case.pk)
                for _ in range(2):
                    self.process(turn)
                    self.assertEqual(turn.status, "running", turn.error_code)
            TextEvidence.objects.filter(pk=record.pk).update(
                body=record.body[:-1] + "."
            )
            calls = model.call_count
            self.process(turn)
            self.assertEqual(model.call_count, calls)
        self.assertEqual((turn.status, turn.error_code), ("failed", "source_changed"))
        saved.refresh_from_db()
        self.assertNotIn("verified_body", saved.locator)

    def test_literal_citation_budget_is_shared_by_derived_surfaces(self):
        owner = self.users["owner"]
        document = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=owner,
            title="Synthetic dispatch",
            file=SimpleUploadedFile("dispatch.txt", b"Synthetic dispatch"),
        )
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=owner,
            operation="native_text",
            method_version="1",
            inputs=snapshot_sources(
                self.case, [{"kind": "document", "id": document.pk}]
            ),
            status="complete",
        )
        result = AnalysisResult.objects.create(case=self.case, run=run)
        for index in range(2):
            ExtractedPassage.objects.create(
                case=self.case,
                result=result,
                kind="document",
                evidence_id=document.pk,
                evidence_sha256=document.sha256,
                text="Mica " * 750,
                item_index=index,
            )
        turn = self.ask("List every record that mentions Mica by name.")
        with patch.object(ai, "infer", side_effect=AssertionError("literal lookup")):
            self.finish(turn)
        self.assertEqual(turn.plan["matched_records"], 1)
        self.assertEqual(turn.sources.filter(status="match").count(), 2)
        self.assertEqual(turn.hits.count(), full_review.LITERAL_CITATIONS_PER_RECORD)

    def test_exhaustive_windows_overlap_and_failure_retries_without_duplicates(self):
        self.source(" " * 1300 + "pickup registered at Gate C. " + " " * 2500)
        turn = self.ask("Check every record for pickup")
        calls = 0

        def infer(turn, authorize, prompt, schema):
            nonlocal calls
            authorize()
            data = json.loads(prompt.split("\n", 1)[1])
            if schema is engine.REVIEW_DECISION:
                return decision("answer")
            calls += 1
            if calls == 2:
                raise AnalysisError("local_ai_busy")
            rows = []
            for row in data["records"]:
                quote = "pickup registered at Gate C."
                idx = row["text"].find(quote)
                refs = (
                    [{"id": row["id"], "quote": quote, "start": idx}]
                    if idx >= 0
                    else []
                )
                rows.append(
                    {
                        "id": row["id"],
                        "outcome": "match" if refs else "miss",
                        "citations": refs,
                    }
                )
            return {"records": rows}

        with patch.object(ai, "infer", side_effect=infer):
            self.process(turn)
            self.assertEqual(turn.mode, "full")
            old_token = str(turn.delivery_token)
            self.process(turn)
            jobs.process_turn(str(turn.pk), old_token)
            self.process(turn)
            self.assertEqual(
                (turn.status, turn.error_code), ("failed", "local_ai_busy")
            )
            turn = services.retry_turn(
                self.users["editor"], self.case.pk, turn.chat_id, turn.pk
            )
            self.finish(turn)
        self.assertEqual(turn.hits.count(), 1)
        self.assertEqual(turn.hits.get().start, 1300)
        self.assertEqual(turn.plan["matched_records"], 1)
        self.assertEqual(turn.total, turn.reviewed)

    def test_refinement_resumes_after_failure_and_finds_a_second_source(self):
        alias = self.source("The shuttle was called Heron.", title="Dispatch")
        destination = self.source("Heron arrived at Pier 19.", title="Harbour log")
        turn = self.ask("Where did the shuttle arrive?")

        def infer(active, authorize, prompt, schema):
            authorize()
            data = json.loads(prompt.split("\n", 1)[1])
            self.assertEqual(data["question"], turn.question)
            self.assertIs(schema, engine.DECISION)
            if not data["refinement_used"]:
                self.assertTrue(
                    any("called Heron" in r["text"] for r in data["records"])
                )
                return decision("refine", "Heron")
            quote = "Heron arrived at Pier 19."
            row = next(r for r in data["records"] if quote in r["text"])
            return decision(
                "answer",
                claims=[
                    {
                        "text": "The log records an arrival at Pier 19.",
                        "citations": [
                            {
                                "id": row["id"],
                                "start": row["text"].index(quote),
                                "quote": quote,
                            }
                        ],
                    }
                ],
            )

        with patch.object(ai, "infer", side_effect=infer) as model:
            self.process(turn)
            self.assertEqual(turn.plan["stage"], "refine")
            model.assert_called_once()
            self.assertTrue(turn.sources.filter(evidence_id=alias.pk).exists())
            self.assertFalse(turn.answer)
            token = turn.delivery_token
            with patch.object(ai, "infer", side_effect=AnalysisError("local_ai_busy")):
                self.process(turn)
            self.assertEqual((turn.status, turn.plan["stage"]), ("failed", "refine"))
            turn = services.retry_turn(
                self.users["editor"], self.case.pk, turn.chat_id, turn.pk
            )
            jobs.process_turn(turn.pk, token)
            self.finish(turn)
            self.assertEqual(model.call_count, 2)
        self.assertEqual(turn.sources.filter(evidence_id=destination.pk).count(), 1)
        self.assertIn(
            "Pier 19", services.inspect_turn(turn)["claims"][0]["citations"][0]["quote"]
        )
        self.assertNotIn("refine_query", turn.plan)
        self.assertEqual(turn.plan["timing"]["model_calls"], 2)

    def test_three_passes_resolve_a_chain_with_quotes_from_each_step(self):
        facts = (
            "The parcel manifest uses codename Quill.",
            "Quill appears on transfer ledger number Vespin.",
            "Vespin lists the destination as Alder Pier.",
        )
        for fact in facts:
            self.source(fact, title="Register")
        turn = self.ask("Where did the parcel go?")
        budgets = []

        def infer(active, authorize, prompt, schema):
            authorize()
            data = json.loads(prompt.split("\n", 1)[1])
            budgets.append(data["searches_remaining"])
            records = data["records"]
            self.assertLessEqual(len(records), 10)
            if len(budgets) <= 2:
                self.assertTrue(
                    any(facts[len(budgets) - 1] in r["text"] for r in records)
                )
                return decision("refine", ("Quill", "Vespin")[len(budgets) - 1])
            citations = []
            for quote in facts:
                row = next(r for r in records if quote in r["text"])
                citations.append(
                    dict(id=row["id"], quote=quote, start=row["text"].index(quote))
                )
            return decision(
                "answer",
                claims=[
                    dict(
                        text="The linked destination is Alder Pier.",
                        citations=citations,
                    )
                ],
            )

        with patch.object(ai, "infer", side_effect=infer):
            self.finish(turn)
        self.assertEqual(budgets, [2, 1, 0])
        refs = services.inspect_turn(turn)["claims"][0]["citations"]
        self.assertCountEqual([r["quote"] for r in refs], facts)
        self.assertTrue(all(r["url"] for r in refs))

    def test_weak_outline_without_a_relevant_child_publishes_the_cited_conflict(self):
        from core.case_search.parents import VERSION, anchor
        from core.models import SearchParent

        readings = ["Pump report recorded 41 kPa.", "Pump correction recorded 37 kPa."]
        for report in readings:
            self.source(report)
        child = self.source("The workshop gauge arrived.", title="Delivery")
        outline = SearchParent.objects.create(
            case=self.case,
            text=child,
            version=VERSION,
            label="Document section",
            outline="Signed pressure gauge calibration",
            anchors=[anchor("text", child.pk, 0, child.body)],
        )
        turn = self.ask("Which pump reading was confirmed?")

        def infer(active, authorize, prompt, schema):
            authorize()
            rows = json.loads(prompt.split("\n", 1)[1])["records"]
            claims = []
            for quote in readings:
                row = next(r for r in rows if quote in r["text"])
                claims.append(
                    {
                        "text": quote,
                        "citations": [
                            {
                                "id": row["id"],
                                "quote": quote,
                                "start": row["text"].index(quote),
                            }
                        ],
                    }
                )
            return decision(
                "refine",
                "signed pressure gauge calibration",
                claims=claims,
                missing="An independent measurement of the pump pressure.",
                independent=True,
            )

        with (
            patch.object(ai, "infer", side_effect=infer) as model,
            patch.object(
                turn_retrieval, "advances_lookup", wraps=turn_retrieval.advances_lookup
            ) as advances,
        ):
            self.process(turn)
            self.assertEqual(
                turn.plan["refinement"]["missing_fact"],
                "An independent measurement of the pump pressure.",
            )
            self.assertFalse(turn.answer)
            self.finish(turn)
        selected = advances.call_args.args[1]
        child_window = next(w for w in selected if w.locator["pk"] == str(child.pk))
        self.assertIn(str(outline.pk), child_window.locator["search_parents"])
        self.assertFalse(child_window.semantic_match)
        model.assert_called_once()
        result = services.inspect_turn(turn)
        self.assertIn("independent confirmation", result["summary"])
        self.assertCountEqual(
            readings, [c["citations"][0]["quote"] for c in result["claims"]]
        )
        self.assertNotIn("refinement", turn.plan)
        self.assertEqual(turn.plan["timing"]["retrieval_calls"], 2)

    def test_queued_legacy_refinement_without_a_partial_answer_still_synthesizes(self):
        self.source("The shuttle ran on Monday.")
        turn = self.ask("Which shuttle was it?")
        with patch.object(ai, "infer", return_value=decision("refine", "Heron")):
            self.process(turn)
        turn.plan["refine_query"] = turn.plan.pop("refinement")["query"]
        turn.save(update_fields=["plan"])
        with patch.object(ai, "infer", return_value=decision("answer")) as model:
            self.finish(turn)
        model.assert_called_once()
        self.assertIn("does not establish an answer", turn.answer["summary"])

    def test_refinement_requires_a_purpose_and_validates_its_partial_citations(self):
        self.source("The pump was running.")
        bad_claim = [
            {
                "text": "The pressure was 70.",
                "citations": [{"id": 0, "quote": "70", "start": 0}],
            }
        ]
        for reply, code in (
            (
                decision("refine", "signed gauge log", missing=""),
                "model_result_structure",
            ),
            (
                decision("refine", "signed gauge log", claims=bad_claim),
                "model_quote_mismatch",
            ),
        ):
            with self.subTest(code=code):
                turn = self.ask("Which pump pressure was recorded?")
                with patch.object(ai, "infer", return_value=reply):
                    self.process(turn)
                self.assertEqual((turn.status, turn.error_code), ("failed", code))
                self.assertFalse(turn.answer)
                self.assertNotIn("refinement", turn.plan)

    def test_answer_can_report_cited_facts_and_a_remaining_question(self):
        quote = "The observer recorded the arrival at 08:10."
        self.source(quote)
        question = "Which arrival did dispatch record?"
        claim = {"text": quote, "citations": [{"id": 0, "quote": quote, "start": 0}]}
        for action in ("answer", "ask"):
            with self.subTest(action=action):
                turn = self.ask("Compare the arrival accounts.")
                reply = decision(
                    action, question if action == "ask" else "", claims=[claim]
                )
                if action == "answer":
                    reply["clarification"] = question
                with patch.object(ai, "infer", return_value=reply) as model:
                    self.finish(turn)
                model.assert_called_once()
                self.assertEqual(turn.answer["claims"][0]["text"], quote)
                self.assertEqual(turn.answer["lead"], question)

    def test_budget_exhaustion_preserves_a_previously_verified_quote(self):
        quote = "The shuttle arrived at 09:15."
        original = self.source(quote)
        turn = self.ask("When did the shuttle arrive?")
        claim = {"text": quote, "citations": [{"id": 0, "quote": quote, "start": 0}]}
        with patch.object(
            ai,
            "infer",
            return_value=decision("refine", "station log", claims=[claim]),
        ):
            self.process(turn)
        self.assertEqual(turn.plan["stage"], "refine")
        with (
            patch.object(
                turn_retrieval,
                "rank_windows",
                side_effect=AnalysisError("chat_budget_exhausted"),
            ),
            patch.object(ai, "infer") as model,
        ):
            self.finish(turn)
        model.assert_not_called()
        answer = services.inspect_turn(turn)
        self.assertIn("Processing limit", answer["summary"])
        cite = answer["claims"][0]["citations"][0]
        self.assertEqual(cite["quote"], quote)
        self.assertIn(str(original.pk), cite["url"])
        self.assertNotIn("refinement", turn.plan)

    def test_budget_exhaustion_before_first_answer_reports_limited_coverage(self):
        turn = self.ask("Where did the shuttle arrive?")
        with patch.object(
            ai, "infer", side_effect=AnalysisError("chat_budget_exhausted")
        ):
            self.finish(turn)
        self.assertEqual(turn.mode, "ordinary")
        self.assertFalse(turn.answer["claims"])
        self.assertIn("Processing limit", turn.answer["summary"])

    def test_missing_timing_value_leaves_a_qualified_gap(self):
        turn = self.ask("What was the median response interval?")
        with patch.object(
            ai,
            "infer",
            return_value=decision(
                "read_timing", "response interval", missing="The saved median."
            ),
        ) as model:
            self.finish(turn)
        model.assert_called_once()
        self.assertEqual(turn.mode, "ordinary")
        self.assertIn("could not verify a saved timing value", turn.answer["summary"])
        self.assertFalse(turn.answer["claims"])
        self.assertFalse(AnalysisRun.objects.filter(case=self.case).exists())

    def test_refinement_limit_returns_an_evidence_gap_without_starting_full_review(
        self,
    ):
        self.source("Only a shuttle was reported.")
        turn = self.ask("Where did the shuttle arrive?")
        with patch.object(
            ai, "infer", return_value=decision("refine", "Heron")
        ) as model:
            self.finish(turn)
        model.assert_called_once()
        self.assertEqual(turn.mode, "ordinary")
        self.assertFalse(turn.answer["claims"])
        self.assertIn("does not establish", turn.answer["summary"])

    def test_two_empty_searches_finish_without_a_second_answer_call(self):
        turn = self.ask("Where did the shuttle arrive?")
        with patch.object(
            ai, "infer", return_value=decision("refine", "Heron")
        ) as model:
            self.finish(turn)
        model.assert_called_once()
        self.assertIn("No relevant retained material", turn.answer["summary"])
        self.assertFalse(turn.sources.exists())

    def test_refinement_withholds_source_derived_query_after_archive(self):
        from django.utils import timezone

        record = self.source("Shuttle code Heron.")
        turn = self.ask("Where did the shuttle arrive?")
        with patch.object(ai, "infer", return_value=decision("refine", "Heron")):
            self.process(turn)
        self.assertEqual(turn.plan["stage"], "refine")
        record.is_archived = True
        record.archive_changed_at = timezone.now()
        record.archive_changed_by = self.users["owner"]
        record.save(
            update_fields=["is_archived", "archive_changed_at", "archive_changed_by"]
        )
        with (
            patch("core.evidence_chat.retrieval_route.related_for_turn") as related,
            patch.object(ai, "infer") as model,
        ):
            self.process(turn)
        related.assert_not_called()
        model.assert_not_called()
        self.assertEqual(turn.error_code, "source_changed")

    def test_rewritten_full_review_rechecks_other_sources_at_dispatch(self):
        from django.utils import timezone

        first = self.ask("Check every record for matches. type:text on:2026-01-15")
        with patch.object(ai, "infer", return_value=decision("ask", "Which dispatch?")):
            self.finish(first)
        originals = [
            self.source("Harbour dispatch recorded Northgate under the code Vireo."),
            self.source("Vireo reached East Pier.", title="Waybill"),
        ]
        other = self.source(
            "Harbour dispatch Vireo recorded an out-of-scope departure."
        )
        day = datetime(2026, 1, 15, 12, tzinfo=UTC)
        TextEvidence.objects.filter(pk__in=[row.pk for row in originals]).update(
            collected_at=day
        )
        TextEvidence.objects.filter(pk=other.pk).update(
            collected_at=day.replace(day=16)
        )
        turn = self.ask("The harbour dispatch.", chat_id=first.chat_id)
        with patch.object(
            ai,
            "infer",
            side_effect=[
                decision("refine", "Vireo"),
                decision(
                    "review_answer", "Explain all Northgate and East Pier matches."
                ),
            ],
        ) as model:
            self.process(turn)
            self.process(turn)
        self.assertEqual(model.call_count, 2)
        for call in model.call_args_list:
            records = json.loads(call.args[2].split("\n", 1)[1])["records"]
            self.assertTrue(records)
            self.assertFalse(any(other.body in row["text"] for row in records))
        self.assertEqual(turn.plan["stage"], "review")
        self.assertEqual(len(turn.plan["review_question_sources"]), 2)
        self.assertEqual(turn.total, 2)
        checked_review = full_review._checked_review

        def change_source_after_batch_read(active, windows, spans):
            self.assertEqual(len(windows), 1)
            other = next(
                row
                for row in originals
                if str(row.pk) != windows[0].locator["evidence_id"]
            )
            other.is_archived = True
            other.archive_changed_at = timezone.now()
            other.archive_changed_by = self.users["owner"]
            other.save(
                update_fields=[
                    "is_archived",
                    "archive_changed_at",
                    "archive_changed_by",
                ]
            )
            return checked_review(active, windows, spans)

        with (
            patch.object(full_review, "REVIEW_BATCH", 1),
            patch.object(
                full_review,
                "_checked_review",
                side_effect=change_source_after_batch_read,
            ) as review,
            patch("core.analysis.local.build_opener") as transport,
        ):
            self.process(turn)
        review.assert_called_once()
        transport.assert_not_called()
        self.assertEqual(turn.error_code, "source_changed")
        self.assertFalse(turn.answer)

    def test_legacy_refinement_does_not_send_unscoped_clarification_context(self):
        self.source("Harbour dispatch recorded code Vireo.")
        first = self.ask("Review every source about the dispatch. type:text")
        with patch.object(ai, "infer", return_value=decision("ask", "Which dispatch?")):
            self.finish(first)
        turn = self.ask("The harbour dispatch.", chat_id=first.chat_id)
        with patch.object(ai, "infer", return_value=decision("refine", "Vireo")):
            self.process(turn)
        self.assertEqual(turn.plan.pop("scoped_clarification"), str(first.pk))
        turn.save(update_fields=["plan"])
        with patch.object(corpus, "rank") as rank, patch.object(ai, "infer") as model:
            self.process(turn)
        rank.assert_not_called()
        model.assert_not_called()
        self.assertEqual(turn.error_code, "chat_review_scope_retired")
        self.assertEqual(review_restart_code(turn), turn.error_code)

    def test_typed_date_and_participant_counts_with_missing_date(self):
        other = Person.objects.create(case=self.case, first_name="Adrian")
        for hour in (1, 19, None):
            PhoneCallEvidence.objects.create(
                case=self.case,
                uploaded_by=self.users["owner"],
                title="Synthetic call",
                sender=self.person,
                recipient=other,
                occurred_at=datetime(2024, 5, 4, hour, tzinfo=UTC) if hour else None,
            )
        distant = PhoneCallEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Other party",
            sender=self.person,
            recipient=self.person,
            occurred_at=datetime(2024, 5, 4, 1, tzinfo=UTC),
        )
        with patch.object(
            ai, "infer", side_effect=AssertionError("deterministic count")
        ):
            from django.utils import timezone

            with timezone.override("America/Toronto"):
                turn = self.ask(
                    "How many calls between Adrian and Synthetic person on 2024-05-03?"
                )
            self.finish(turn)
            ambiguous = self.ask(
                "How many calls between Adrian and Synthetic person on Friday?"
            )
            self.finish(ambiguous)
        self.assertEqual(ambiguous.mode, "clarification")
        self.assertFalse(turn.sources.filter(evidence_id=distant.pk).exists())
        coverage = turn.plan["recorded_selection"]["coverage"]
        self.assertEqual(coverage["matched"], 1)
        self.assertEqual(coverage["unknown_time"], 1)
        self.assertEqual(turn.selection_members.count(), 2)
        self.assertEqual(turn.sources.count(), 1)

    def test_invalid_quotes_and_revoked_editor(self):
        self.source("Pickup at the north gate.")
        turn = self.ask("Where was the pickup?")
        with patch.object(
            ai,
            "infer",
            return_value=decision(
                "answer",
                claims=[
                    {
                        "text": "Wrong",
                        "citations": [{"id": 0, "quote": "Wrong", "start": 0}],
                    }
                ],
            ),
        ):
            self.process(turn)
        self.assertEqual(turn.error_code, "model_quote_mismatch")
        self.assertFalse(turn.answer)
        turn = services.retry_turn(
            self.users["editor"], self.case.pk, turn.chat_id, turn.pk
        )
        CaseMembership.objects.filter(
            case=self.case, user=self.users["editor"]
        ).delete()
        self.process(turn)
        self.assertEqual(turn.error_code, "access_removed")

    @override_settings(PANDECT_AI_GATEWAY=OPENAI)
    def test_external_chat_requires_separate_grant_and_tracks_turn_usage(self):
        self.source("The pickup moved to Friday.")
        self.case.ai_backend = "gateway"
        self.case.save(update_fields=["ai_backend"])
        routes = configured_external_executions(case=self.case)
        approve_case_execution(
            self.case,
            self.users["owner"],
            {key: value for key, value in routes.items() if key != "evidence_chat"},
        )
        with self.assertRaisesMessage(
            AnalysisError, "external_processing_not_approved"
        ):
            self.ask("When was the pickup?")
        approve_case_execution(self.case, self.users["owner"], routes)
        turn = self.ask("When was the pickup?")

        def first_reply(*args, **kwargs):
            revoke_case_execution(self.case)
            return response(openai_reply(decision("refine", "Friday pickup")))

        with patch("core.analysis.gateway.build_opener") as build:
            build.return_value.open.side_effect = first_reply
            self.process(turn)
        self.assertEqual(turn.error_code, "external_processing_not_approved")
        self.assertEqual(build.return_value.open.call_count, 1)
        usage = GatewayRequest.objects.get(case=self.case, chat_turn=turn)
        self.assertEqual(usage.operation, "evidence_chat")

    def test_full_review_freezes_all_501_records_and_excludes_later_upload(self):
        import hashlib

        rows = [
            TextEvidence(
                case=self.case,
                uploaded_by=self.users["owner"],
                title=f"Record {i}",
                body="Synthetic testimony",
                sha256=hashlib.sha256(b"Synthetic testimony").hexdigest(),
            )
            for i in range(501)
        ]
        TextEvidence.objects.bulk_create(rows)
        turn = self.ask("List all text records")
        self.process(turn)
        self.assertEqual(turn.total, 501)
        late = self.source("A later upload")
        self.finish(turn)
        self.assertEqual(turn.plan["matched_records"], 501)
        self.assertEqual(turn.hits.count(), 501)
        self.assertFalse(turn.sources.filter(evidence_id=late.pk).exists())

    def test_full_review_honors_type_and_date_before_any_model_batch(self):
        first = self.source("Pickup appears in the selected report.")
        other_day = self.source("Pickup appears in a different day's report.")
        day = datetime(2026, 1, 15, 12, tzinfo=UTC)
        TextEvidence.objects.filter(pk=first.pk).update(collected_at=day)
        TextEvidence.objects.filter(pk=other_day.pk).update(
            collected_at=day.replace(day=16)
        )
        DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Pickup appears in another evidence type",
            file=SimpleUploadedFile("source.txt", b"Synthetic content"),
            collected_at=day,
        )
        turn = self.ask("Check every record for pickup type:text on:2026-01-15")

        def infer(turn, authorize, prompt, schema):
            authorize()
            records = json.loads(prompt.split("\n", 1)[1])["records"]
            self.assertEqual(len(records), 1)
            self.assertIn(first.body, records[0]["text"])
            if schema is engine.REVIEW_DECISION:
                return decision("answer")  # The explicit enumeration forces a review.
            self.assertIs(schema, engine.REVIEW)
            return {
                "records": [
                    {"id": records[0]["id"], "outcome": "miss", "citations": []}
                ]
            }

        with patch.object(ai, "infer", side_effect=infer) as model:
            self.finish(turn)
        self.assertEqual(model.call_count, 2)
        self.assertEqual((turn.mode, turn.total, turn.reviewed), ("full", 1, 1))
        self.assertEqual(turn.sources.get().evidence_id, first.pk)

    def test_legacy_scoped_question_cannot_resume_an_unfiltered_manifest(self):
        turn = self.ask("Check every record for pickup type:text")
        turn.mode = "full"
        turn.plan = {"stage": "review"}
        turn.save(update_fields=("mode", "plan"))
        with patch.object(ai, "infer") as model:
            self.process(turn)
        model.assert_not_called()
        self.assertEqual(
            (turn.status, turn.error_code), ("failed", "chat_review_scope_retired")
        )
        with self.assertRaisesMessage(AnalysisError, "chat_review_scope_retired"):
            services.retry_turn(
                self.users["editor"], self.case.pk, turn.chat_id, turn.pk
            )
        turn.status = "complete"
        self.assertIsNone(services.inspect_turn(turn))

    def test_message_review_limits_initial_clarified_and_review_batches(self):
        recipient = Person.objects.create(case=self.case, first_name="Mica")
        message = TextMessageEvidence.objects.create(
            case=self.case,
            sender=self.person,
            recipient=recipient,
            uploaded_by=self.users["owner"],
            title="Pickup",
            body="Pickup at the south gate.",
        )
        DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Pickup confidential document",
            file=SimpleUploadedFile("source.txt", b"Synthetic content"),
        )

        def infer(active, authorize, prompt, schema):
            authorize()
            rows = json.loads(prompt.split("\n", 1)[1])["records"]
            self.assertEqual(len(rows), 1)
            self.assertIn(message.body, rows[0]["text"])
            self.assertNotIn("confidential document", prompt)
            if schema is engine.REVIEW:
                return {
                    "records": [
                        {"id": rows[0]["id"], "outcome": "miss", "citations": []}
                    ]
                }
            self.assertIs(schema, engine.REVIEW_DECISION)
            if active.question.startswith("After"):
                return decision("ask", "Which pickup?")
            return decision("review", active.question)

        with patch.object(ai, "infer", side_effect=infer) as model:
            direct = self.ask("Inspect every message for pickup.")
            self.finish(direct)
            typed = self.ask("Inspect every message for pickup type:text-message")
            self.finish(typed)
            first = self.ask("After checking every message, explain the pickup.")
            self.finish(first)
            self.assertEqual(first.mode, "clarification")
            follow = self.ask("The south gate pickup.", chat_id=first.chat_id)
            self.finish(follow)
            self.assertEqual(model.call_count, 7)
        for turn in (direct, follow):
            self.assertEqual((turn.mode, turn.total), ("full", 1))
            self.assertEqual(turn.sources.get().evidence_id, message.pk)
            # Legacy manifests could also contain documents, including when the
            # investigator answered a clarification without repeating "messages".
            turn.plan["review_version"] = 1
            self.assertIsNone(services.inspect_turn(turn))
        typed.plan["review_version"] = 1
        self.assertTrue(services.reply_available(typed))

    def test_conflicting_message_scope_clarifies_and_legacy_refinement_is_withheld(
        self,
    ):
        turn = self.ask("Inspect every message for pickup type:document")
        with patch.object(ai, "infer") as model:
            self.finish(turn)
            model.assert_not_called()
        self.assertEqual(turn.mode, "clarification")
        self.assertFalse(turn.sources.exists())
        turn = self.ask("After inspecting every message, explain the pickup.")
        with patch.object(ai, "infer", return_value=decision("refine", "dispatch")):
            self.process(turn)
        self.assertEqual(turn.plan["stage"], "refine")
        turn.plan.pop("review_version")
        turn.save(update_fields=["plan"])
        with patch.object(ai, "infer") as model:
            self.process(turn)
            model.assert_not_called()
        self.assertEqual(turn.error_code, "chat_review_scope_retired")

    def test_archived_context_cannot_leave_case_on_active_only_followup(self):
        from django.utils import timezone

        original = self.source("Unique synthetic source wording.")
        first = self.ask("When was the pickup?")
        with patch.object(
            ai,
            "infer",
            return_value=decision(
                "answer",
                claims=[
                    {
                        "text": "Unique synthetic source wording.",
                        "citations": [
                            {
                                "id": 0,
                                "quote": "Unique synthetic source wording.",
                                "start": 0,
                            }
                        ],
                    }
                ],
            ),
        ):
            self.finish(first)
        original.is_archived = True
        original.archive_changed_at = timezone.now()
        original.archive_changed_by = self.users["owner"]
        original.save(
            update_fields=["is_archived", "archive_changed_at", "archive_changed_by"]
        )
        second = self.ask("Can you clarify?", chat_id=first.chat_id)

        def respond(turn, authorize, prompt, schema):
            authorize()
            self.assertNotIn("Unique synthetic source wording.", prompt)
            return decision("ask", "Which source should I check?")

        with patch.object(ai, "infer", side_effect=respond):
            self.finish(second)
        self.assertEqual(second.mode, "clarification")

    def test_recent_reply_context_is_withheld_after_its_source_is_archived(self):
        from django.utils import timezone

        original = self.source("Unique synthetic source wording.")
        first = self.ask("What does this source say?")
        with patch.object(
            ai,
            "infer",
            return_value=decision(
                "answer",
                claims=[
                    {
                        "text": original.body,
                        "citations": [{"id": 0, "quote": original.body, "start": 0}],
                    }
                ],
            ),
        ):
            self.finish(first)
        second = self.ask("Can you clarify? type:image", chat_id=first.chat_id)

        def respond(turn, authorize, prompt, schema):
            authorize()
            payload = json.loads(prompt.split("\n", 1)[1])
            self.assertIn(original.body, payload["history"][-1]["reply_context"])
            self.assertEqual(payload["records"], [])
            return decision("ask", "Can you verify the synthetic source wording?")

        with patch.object(ai, "infer", side_effect=respond):
            self.finish(second)
        self.assertTrue(services.reply_available(second))
        saved_plan = second.plan
        second.plan = {
            key: value
            for key, value in saved_plan.items()
            if key not in ("history_sources_pinned", "recalled_sources")
        }
        self.assertFalse(services.reply_available(second))
        second.plan = saved_plan
        TextEvidence.objects.filter(pk=original.pk).update(
            is_archived=True,
            archive_changed_at=timezone.now(),
            archive_changed_by=self.users["owner"],
        )
        self.assertFalse(services.reply_available(second))
        TextEvidence.objects.filter(pk=original.pk).update(is_archived=False)
        self.assertTrue(services.reply_available(second))

    def test_partial_window_response_cannot_publish_as_full_coverage(self):
        self.source("Pickup at the south gate.")
        turn = self.ask("Review every record for the pickup")
        with patch.object(
            ai,
            "infer",
            side_effect=[
                decision("review", turn.question),
                {"records": []},
            ],
        ):
            self.process(turn)
            self.process(turn)
        self.assertEqual(turn.error_code, "model_result_structure")
        self.assertEqual(turn.reviewed, 0)
        self.assertFalse(turn.answer)

    def test_exhaustive_narrative_includes_cited_answer_and_complete_match_list(self):
        self.source("The north gate pickup was cancelled.")
        self.source("The south gate pickup was confirmed.")
        turn = self.ask("After checking every record, explain the pickup change.")

        def infer(turn, authorize, prompt, schema):
            authorize()
            data = json.loads(prompt.split("\n", 1)[1])
            if schema is engine.REVIEW_DECISION:
                return decision("review_answer", turn.question)
            refs = []
            for row in data["records"]:
                quote = row["text"].split("\n", 1)[0]
                refs.append({"id": row["id"], "quote": quote, "start": 0})
            if schema is engine.REVIEW:
                return {
                    "records": [
                        {"id": ref["id"], "outcome": "match", "citations": [ref]}
                        for ref in refs
                    ]
                }
            return {
                "claims": [
                    {
                        "text": "One pickup was cancelled and one was confirmed.",
                        "citations": refs,
                    }
                ],
                "clarification": "",
                "lead": "",
            }

        with patch.object(ai, "infer", side_effect=infer):
            self.finish(turn)
        self.assertEqual(turn.plan["matched_records"], 2)
        self.assertEqual(len(services.inspect_turn(turn)["claims"][0]["citations"]), 2)
        self.assertIn("2 representative", turn.answer["summary"])

    def test_followup_to_full_review_uses_only_current_source_excerpts(self):
        from django.utils import timezone

        archived = self.source("Pickup moved to Blue Pier.", "Blue Pier dispatch")
        active = self.source("Pickup moved to Red Pier.", "Red Pier dispatch")
        first = self.ask("Check every record for pickup")

        def review(turn, authorize, prompt, schema):
            authorize()
            if schema is engine.REVIEW_DECISION:
                return decision("review", turn.question)
            records = []
            for item in json.loads(prompt.split("\n", 1)[1])["records"]:
                quote = item["text"].split("\n", 1)[0]
                records.append(
                    {
                        "id": item["id"],
                        "outcome": "match",
                        "citations": [{"id": item["id"], "start": 0, "quote": quote}],
                    }
                )
            return {"records": records}

        with patch.object(ai, "infer", side_effect=review):
            self.finish(first)
        self.assertEqual(first.hits.count(), 2)
        archived.is_archived = True
        archived.archive_changed_at = timezone.now()
        archived.archive_changed_by = self.users["owner"]
        archived.save(
            update_fields=["is_archived", "archive_changed_at", "archive_changed_by"]
        )

        second = self.ask("Who sent the dispatch you found?", chat_id=first.chat_id)

        def followup(turn, authorize, prompt, schema):
            authorize()
            self.assertIs(schema, engine.DECISION)
            self.assertNotIn("Blue Pier", prompt)
            history = json.loads(prompt.split("\n", 1)[1])["history"][-1]
            self.assertIn("Red Pier dispatch", history["reply_context"])
            self.assertIn("Pickup moved to Red Pier.", history["reply_context"])
            return decision("ask", "Which sender identifier?")

        with patch.object(ai, "infer", side_effect=followup):
            self.finish(second)
        self.assertEqual(second.mode, "clarification")
        self.assertTrue(services.reply_available(second))
        TextEvidence.objects.filter(pk=active.pk).update(
            is_archived=True,
            archive_changed_at=timezone.now(),
            archive_changed_by=self.users["owner"],
        )
        self.assertFalse(services.reply_available(second))

    def test_saved_finding_leads_to_its_cited_original_across_deliveries(self):
        self.source("The ballast manifest refers to the copper ledger.", "Manifest")
        quote = "Discharged at Alder Pier."
        original = self.source(quote, "Dock slip")
        inputs = snapshot_sources(self.case, [{"kind": "text", "id": original.pk}])
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.users["owner"],
            operation="entities",
            inputs=inputs,
            status="complete",
            method_version="1",
        )
        finding_text = "The copper ledger identifies a dock slip."
        result = AnalysisResult.objects.create(
            run=run,
            case=self.case,
            data={
                "items": [
                    {
                        "label": "Ledger reference",
                        "text": finding_text,
                        "source_ids": [inputs[0]["id"]],
                        "anchors": [],
                        "values": {},
                    }
                ]
            },
        )
        project_result(result)
        finding = result.findings.get()
        turn = self.ask("Where did the ballast arrive?")

        def infer(active, authorize, prompt, schema):
            authorize()
            self.assertIs(schema, engine.DECISION)
            rows = json.loads(prompt.split("\n", 1)[1])["records"]
            lookup = active.plan.get("refinement", {}).get("lookup")
            if lookup is None:
                self.assertTrue(any("ballast manifest" in r["text"] for r in rows))
                self.assertFalse(any(finding_text in r["text"] for r in rows))
                return decision(
                    "search_findings", "copper ledger", missing="The ledger reference."
                )
            if lookup == "search_findings":
                row = next(r for r in rows if finding_text in r["text"])
                self.assertFalse(any(quote in r["text"] for r in rows))
                return decision(
                    "follow_source",
                    "discharge point",
                    missing="Destination on the dock slip.",
                ) | {"source": row["id"]}
            self.assertEqual(lookup, "follow_source")
            return self.answer_with_quote(rows, quote)

        with patch.object(ai, "infer", side_effect=infer) as model:
            self.process(turn)
            self.assertEqual(turn.plan["refinement"]["lookup"], "search_findings")
            token = turn.delivery_token
            self.process(turn)
            ref = turn.plan["refinement"]
            self.assertEqual(ref["lookup"], "follow_source")
            self.assertEqual(
                turn.sources.get(key=ref["source_key"]).locator["pk"], str(finding.pk)
            )
            jobs.process_turn(str(turn.pk), str(token))
            self.finish(turn)
            self.assertEqual(model.call_count, 3)
        citation = services.inspect_turn(turn)["claims"][0]["citations"][0]
        self.assertEqual(citation["quote"], quote)
        self.assertIn(str(original.pk), citation["url"])

    def test_saved_measurement_on_first_lookup_and_refinement(
        self,
    ):
        original = self.source("The larch parcel was sealed.")
        inputs = snapshot_sources(self.case, [{"kind": "text", "id": original.pk}])
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.users["owner"],
            operation="style_profiles",
            method_version="1",
            status="complete",
            inputs=inputs,
        )
        metrics = {"unique_words": 5, "executor_tokens": 10000}
        result = AnalysisResult.objects.create(
            case=self.case, run=run, data={"metrics": metrics}
        )
        project_result(result)
        self.assertFalse(result.findings.exists())
        for question, initial in (
            ("How many unique words were recorded?", True),
            ("What does the available material tell us?", False),
        ):
            turn = self.ask(question)

            def infer(active, authorize, prompt, schema, initial=initial):
                authorize()
                rows = json.loads(prompt.split("\n", 1)[1])["records"]
                if not active.plan.get("refinement") and not initial:
                    return decision(
                        "read_analysis",
                        "unique words",
                        missing="The vocabulary measurement.",
                    )
                return self.answer_with_quote(rows, "Unique words: 5")

            with patch.object(ai, "infer", side_effect=infer):
                self.finish(turn)
            answer = services.inspect_turn(turn)
            citation = answer["claims"][0]["citations"][0]
            self.assertIn(str(run.pk), citation["url"])
            self.assertEqual(
                turn.sources.get(status="context", result_id=result.pk).result_id,
                result.pk,
            )
        self.assertEqual(AnalysisRun.objects.filter(case=self.case).count(), 1)

    def saved_timing_report(self):
        other = Person.objects.create(case=self.case, first_name="Mica")
        messages = [
            TextMessageEvidence.objects.create(
                case=self.case,
                uploaded_by=self.users["owner"],
                title="Exchange",
                sender=sender,
                recipient=recipient,
                body=body,
                occurred_at=datetime(2026, 2, 17, 10, minute, tzinfo=UTC),
            )
            for sender, recipient, body, minute in (
                (self.person, other, "Parcel collected.", 0),
                (other, self.person, "Acknowledged.", 2),
            )
        ]
        inputs = snapshot_sources(
            self.case, [{"kind": "text-message", "id": m.pk} for m in messages]
        )
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.users["owner"],
            operation="comm_patterns",
            inputs=inputs,
            method_version="2",
            status="complete",
        )
        result = AnalysisResult.objects.create(
            case=self.case,
            run=run,
            data=interaction_patterns(resolve_inputs(run, self.case), {}),
        )
        project_result(result)
        return messages, result

    def test_saved_timing_report_answers_without_findings_or_new_analysis(self):
        from django.utils import timezone

        messages, result = self.saved_timing_report()
        self.assertFalse(result.findings.exists())
        turn = self.ask("How quickly did they get back to each other?")
        quote = "Median response interval (seconds): 120.0"

        def infer(active, authorize, prompt, schema):
            authorize()
            self.assertIs(schema, engine.DECISION)
            rows = json.loads(prompt.split("\n", 1)[1])["records"]
            if not active.plan.get("refinement"):
                return decision(
                    "read_timing", "response interval", missing="A saved measurement."
                )
            self.assertEqual(len(rows), 1)
            self.assertIn("Selected messages: 2", rows[0]["text"])
            self.assertFalse(any(m.body in rows[0]["text"] for m in messages))
            return self.answer_with_quote(rows, quote)

        with patch.object(ai, "infer", side_effect=infer) as model:
            self.process(turn)
            self.assertEqual(turn.plan["refinement"]["lookup"], "read_timing")
            self.finish(turn)
            self.assertEqual(model.call_count, 2)
        self.assertSequenceEqual(
            AnalysisRun.objects.filter(case=self.case), [result.run]
        )
        citation = services.inspect_turn(turn)["claims"][0]["citations"][0]
        self.assertEqual(citation["quote"], quote)
        self.assertIn(str(result.run_id), citation["url"])
        self.assertEqual(turn.sources.get().result_id, result.pk)

        newer = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.users["owner"],
            operation="comm_patterns",
            method_version="2",
            inputs=result.run.inputs,
            status="complete",
        )
        published = AnalysisResult.objects.create(
            case=self.case, run=newer, data=result.data
        )
        _, unrelated = self.saved_timing_report()
        counts = AnalysisRun.objects.filter(case=self.case).count()
        followup = self.ask(
            "Has anything else turned up since answer 1?", chat_id=turn.chat_id
        )

        def revised_timing(active, authorize, prompt, schema):
            authorize()
            rows = json.loads(prompt.split("\n", 1)[1])["records"]
            reports = [row for row in rows if row["type"] == "report/comm_patterns"]
            self.assertEqual(len(reports), 1)
            return self.answer_with_quote(reports, quote)

        with patch.object(ai, "infer", side_effect=revised_timing) as model:
            self.finish(followup)
        self.assertEqual(model.call_count, 1)
        refs = services.inspect_turn(followup)["claims"][0]["citations"]
        self.assertIn(str(published.run_id), refs[0]["url"])
        self.assertNotIn(str(unrelated.run_id), refs[0]["url"])
        self.assertEqual(turn.sources.get().result_id, result.pk)
        self.assertEqual(AnalysisRun.objects.filter(case=self.case).count(), counts)
        TextMessageEvidence.objects.filter(pk=messages[0].pk).update(
            is_archived=True,
            archive_changed_at=timezone.now(),
            archive_changed_by=self.users["owner"],
        )
        self.assertIsNone(services.inspect_turn(turn))

    def answer_with_quote(self, rows, quote):
        row = next(r for r in rows if quote in r["text"])
        citation = {"id": row["id"], "start": row["text"].index(quote), "quote": quote}
        return decision("answer", claims=[{"text": quote, "citations": [citation]}])

    def test_full_review_keeps_saved_report_context_separate_from_its_manifest(self):
        from django.utils import timezone

        messages, result = self.saved_timing_report()
        turn = self.ask("Do a full review of the exchange.")

        def infer(active, authorize, prompt, schema):
            authorize()
            rows = json.loads(prompt.split("\n", 1)[1])["records"]
            if schema is engine.REVIEW:
                return {
                    "records": [
                        {"id": row["id"], "outcome": "miss", "citations": []}
                        for row in rows
                    ]
                }
            self.assertIs(schema, engine.REVIEW_DECISION)
            if not active.plan.get("refinement"):
                return decision(
                    "read_timing",
                    "timing",
                    missing="The saved measurement for this exchange.",
                )
            self.assertTrue(
                any("Median response interval" in row["text"] for row in rows)
            )
            return decision("review", turn.question)

        with patch.object(ai, "infer", side_effect=infer):
            self.finish(turn)
        self.assertEqual(turn.mode, "full")
        self.assertEqual(turn.total, len(messages))
        self.assertTrue(services.reply_available(turn))
        context = turn.sources.filter(result_id=result.pk, status="context")
        self.assertEqual(context.update(case=self.other_case), 1)
        self.assertFalse(services.reply_available(turn))
        context.update(case=self.case)
        self.assertTrue(services.reply_available(turn))
        TextMessageEvidence.objects.filter(pk=messages[0].pk).update(
            is_archived=True,
            archive_changed_at=timezone.now(),
            archive_changed_by=self.users["owner"],
        )
        self.assertFalse(services.reply_available(turn))
