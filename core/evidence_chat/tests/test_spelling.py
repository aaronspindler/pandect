from datetime import timedelta

from django.http import Http404
from django.test import TestCase, override_settings
from django.utils import timezone

from core.evidence_chat.corpus import cite, rank, read_window
from core.evidence_chat.spelling import alternatives
from core.testing import EvidenceTestMixin
from geo.models import LocationEvidence
from people.models import Person


@override_settings(PANDECT_SEARCH_EMBEDDING_PROVIDER="disabled")
class SpellingLeadsTests(EvidenceTestMixin, TestCase):
    def test_case_name_typo_opens_exact_original_without_identifier_expansion(self):
        Person.objects.create(case=self.case, first_name="Marisol")
        original = self.create_evidence(
            body="The relay log lists Marisol as the caller."
        )
        ((loc, start),) = rank(self.case, "Who is Marislo?", user=self.viewer)
        self.assertEqual(loc["evidence_id"], str(original.pk))
        self.assertIn("Marisol", read_window(self.case, loc, start)["text"])
        self.assertEqual(
            cite(self.case, loc, original.body.index("Marisol"), "Marisol")["quote"],
            "Marisol",
        )
        for query in (
            '"Marislo"',
            "Marislo@example.test",
            "VIN Marislo",
            "MARISLO",
            "X-Marislo-955",
        ):
            with self.subTest(query=query):
                self.assertFalse(alternatives(self.case, query, ["marislo"], None))
        with self.assertRaises(Http404):
            rank(self.case, "Marislo", user=self.outsider)

    def test_structured_place_and_explicit_filters(self):
        other = self.create_evidence(LocationEvidence, address="Harborton Quay")
        self.create_evidence(body="The ship sailed from Harborton Quay at eight.")
        refs = rank(
            self.case,
            "Harbortan",
            filters={"record_type": "location"},
            user=self.viewer,
        )
        self.assertEqual([row["evidence_id"] for row, _ in refs], [str(other.pk)])
        loc, start = refs[0]
        self.assertIn(
            "Address: Harborton Quay", read_window(self.case, loc, start)["text"]
        )
        self.assertEqual(
            rank(
                self.case,
                "Harbortan",
                through=other.created_at - timedelta(seconds=1),
                user=self.viewer,
            ),
            [],
        )
        LocationEvidence.objects.filter(pk=other.pk).update(
            is_archived=True,
            archive_changed_at=timezone.now(),
            archive_changed_by=self.owner,
        )
        self.assertEqual(rank(self.case, "Harbortan", user=self.viewer), [])

    def test_ambiguous_and_cross_case_names_supply_no_fuzzy_lead(self):
        self.create_evidence(body="Kestrel and Kestrael departed the warehouse.")
        Person.objects.create(case=self.other_case, last_name="Kestrel")
        self.assertFalse(rank(self.case, "Kestral", user=self.viewer))
        Person.objects.create(case=self.case, last_name="Kestrel")
        Person.objects.create(case=self.case, last_name="Kestrael")
        self.assertFalse(rank(self.case, "Kestral", user=self.viewer))
