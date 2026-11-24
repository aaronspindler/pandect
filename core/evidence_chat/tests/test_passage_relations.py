from uuid import UUID

from django.test import SimpleTestCase, TestCase, override_settings

from core.case_search.passage_relations import query_relations, rank_related
from core.evidence_chat.corpus import rank, read_window
from core.testing import EvidenceTestMixin


class PassageRelationTests(SimpleTestCase):
    def test_roles_dates_order_and_uncertainty_follow_the_action(self):
        for query, relevant, distractor in (
            (
                "Which photo was captured Monday and shared Friday?",
                "The photo was taken Monday and sent Friday.",
                "The photo was taken Friday and sent Monday.",
            ),
            (
                "When was the courier's departure approximately?",
                "The courier left around 14:30.",
                "The courier left at 14:30 precisely.",
            ),
            (
                "Which photo was sent at about 09:15?",
                "The photo was sent at about 09:15.",
                "The photo was sent at about 22:45.",
            ),
            (
                "Which sample was sealed before it was weighed?",
                "The sample was sealed before it was weighed.",
                "The sample was sealed after it was weighed.",
            ),
            (
                "Which inspection was not approved Tuesday?",
                "The inspection was not approved Tuesday.",
                "The inspection was approved Tuesday and was not recorded.",
            ),
        ):
            with self.subTest(query=query):
                rows = [{"excerpt": text} for text in (distractor, relevant)]
                ranked = rank_related(query_relations(query), rows)
                self.assertEqual(ranked, rows[::-1])

    def test_repeated_action_can_have_two_different_qualifications(self):
        query = query_relations(
            "Which sample was inspected Monday and not inspected Wednesday?"
        )
        rows = [
            {"excerpt": text}
            for text in (
                "The sample was inspected Monday and inspected Wednesday.",
                "The sample was inspected Monday but was not inspected Wednesday.",
            )
        ]
        self.assertEqual(rank_related(query, rows), rows[::-1])

    def test_dates_in_another_paragraph_do_not_date_the_event(self):
        query = query_relations("Which permit was granted Sunday?")
        rows = [
            {"excerpt": text}
            for text in (
                "The permit was granted.\n\nSunday was an office holiday.",
                "The permit was granted Sunday.",
            )
        ]
        self.assertEqual(rank_related(query, rows), rows[::-1])
        self.assertIsNone(query_relations("Which permit lists the applicant?"))


@override_settings(PANDECT_SEARCH_EMBEDDING_PROVIDER="disabled")
class ChatRelationTests(EvidenceTestMixin, TestCase):
    def test_first_window_has_the_requested_dates_on_the_correct_actions(self):
        first = "The photo was taken Friday and sent Monday."
        second = "The photo was taken Monday and sent Friday."
        item = self.create_evidence(
            id=UUID(int=2), body=first + " shelf" * 260 + second
        )
        self.create_evidence(id=UUID(int=1), body=first)
        hits = [
            {
                "source": ("text", item.pk),
                "kind": "text",
                "start": item.body.index(body),
                "excerpt": body,
            }
            for body in (first, second)
        ]
        query = "Which photo was taken Monday and sent Friday?"
        windows = rank(self.case, query, user=self.owner, related=lambda _: (hits, ""))
        locator, start = windows[0]
        self.assertEqual(locator["pk"], str(item.pk))
        self.assertIn(second, read_window(self.case, locator, start, 900)["text"])
