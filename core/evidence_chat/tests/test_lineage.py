from uuid import UUID

from django.test import TestCase

from core.evidence_chat import corpus, engine
from core.evidence_chat.lineage import corroboration, forwarded_origins
from core.evidence_chat.turn_retrieval import advances_lookup
from core.testing import EvidenceTestMixin
from document.models import DocumentEvidence
from phone.models import CommunicationContext, TextMessageEvidence


class SourceLineageTests(EvidenceTestMixin, TestCase):
    def test_native_forward_marker_agrees_in_counts_and_excerpts(self):
        document = self.create_evidence(DocumentEvidence)
        for row, value in enumerate(("yEs", "True", True, 1, "No"), 1):
            with self.subTest(value=value):
                message = self.create_evidence(TextMessageEvidence, body="Arrival")
                CommunicationContext.objects.create(
                    case=self.case,
                    message=message,
                    source_document=document,
                    source_sha256=document.sha256,
                    source_row=row,
                    source_start=row - 1,
                    source_end=row,
                    parser_version="fixture",
                    timezone="UTC",
                    raw_sender="Observer",
                    source_context={"raw_fields": {"Forwarded": value}},
                )
                loc = next(
                    loc
                    for loc in corpus.discover(self.case, False, review=True)
                    if loc["pk"] == str(message.pk)
                )
                expected = value != "No"
                self.assertEqual(loc["message_lineage"]["forwarded"], expected)
                self.assertEqual(bool(forwarded_origins(self.case, [loc])), expected)

    def test_marked_forward_and_finding_are_not_new_independent_accounts(self):
        text = "Dispatch recorded the arrival at 09:25."
        original = self.create_evidence(TextMessageEvidence, body=text)
        forwarded = self.create_evidence(TextMessageEvidence, body="Fwd: " + text)
        rows = {
            loc["evidence_id"]: loc
            for loc in corpus.discover(self.case, False, review=True)
        }
        loc = rows[str(original.pk)]
        copied = rows[str(forwarded.pk)]
        finding = loc | {"key": "finding", "surface": "finding"}
        summary = corroboration(
            [loc, copied, finding], forwarded_origins(self.case, [loc, copied, finding])
        )
        self.assertEqual(
            summary,
            {"originals": 2, "partial": False, "forwards": 1, "repeated_wording": 1},
        )
        self.assertFalse(
            advances_lookup(
                {
                    "query": "dispatch arrival",
                    "missing_fact": "Independent account of arrival.",
                    "independent_source": True,
                    "answer": {"claims": []},
                },
                [engine.Window(copied, 0, "Fwd: " + text)],
                [engine.Window(loc, 0, text)],
            )
        )
        self.assertTrue(corpus.read_window(self.case, copied, 0, 4))
        self.assertIsNone(
            corpus.read_window(
                self.case, copied | {"message_lineage": loc["message_lineage"]}, 0, 4
            )
        )
        self.assertTrue(
            corpus.read_window(
                self.case,
                {k: v for k, v in copied.items() if k != "message_lineage"},
                0,
                4,
            )
        )

    def test_diverse_search_reserves_room_for_rare_wording_before_the_excerpt_limit(
        self,
    ):
        for i in range(38):
            self.create_evidence(
                TextMessageEvidence,
                pk=UUID(int=i + 1),
                body=(
                    "Aster observer reports 08:10."
                    if i == 37
                    else "Aster dispatch reports 09:25."
                    if i == 0
                    else "Fwd: Aster dispatch reports 09:25."
                ),
            )
        result = corpus.rank(
            self.case,
            "Compare Aster accounts.",
            user=self.owner,
            limit=4,
            related=lambda _: ([], {}),
        )
        ids = {loc["evidence_id"] for loc, _ in result}
        self.assertIn(str(UUID(int=38)), ids)
        self.assertIn(str(UUID(int=1)), ids)
        prior = next(loc for loc, _ in result if loc["pk"] == str(UUID(int=1)))
        independent = corpus.rank(
            self.case,
            "Aster",
            user=self.owner,
            limit=4,
            independent_of=[prior],
            related=lambda _: ([], {}),
        )
        self.assertEqual({loc["pk"] for loc, _ in independent}, {str(UUID(int=38))})

    def test_ambiguous_lookup_keeps_only_grounded_claims_without_dispatch(self):
        message = self.create_evidence(
            TextMessageEvidence, body="Observer recorded arrival at 08:10."
        )
        loc = next(
            locator
            for locator in corpus.discover(self.case, False, review=True)
            if locator["pk"] == str(message.pk)
        )
        window = engine.Window(loc, 0, "Observer recorded arrival at 08:10.")
        partial = engine.verified_partial(
            {
                "clarification": "Do these accounts describe the same delivery?",
                "claims": [
                    {
                        "text": "The observer recorded 08:10.",
                        "citations": [{"id": 0, "start": 29, "quote": "08:10"}],
                    },
                    {
                        "text": "It happened at noon.",
                        "citations": [{"id": 0, "start": 0, "quote": "noon"}],
                    },
                ],
            },
            [window],
        )
        self.assertEqual(
            [claim["text"] for claim in partial["claims"]],
            ["The observer recorded 08:10."],
        )
        self.assertIn("same delivery", partial["lead"])
