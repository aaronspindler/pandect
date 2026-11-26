from django.test import SimpleTestCase

from core.evidence_chat.qualification import ARRIVAL_TIMES, PARCEL_TIMES, claim_supports


class QualificationAttributionTests(SimpleTestCase):
    def test_accepts_attributed_claims_with_compound_sentences_and_reversed_word_order(
        self,
    ):
        for text, expected in (
            (
                "The clerk recorded 8:10, while the witness reported 09:25.",
                ARRIVAL_TIMES,
            ),
            (
                "At 08:10, according to the register; at 09:25 per the witness.",
                ARRIVAL_TIMES,
            ),
            (
                "The parcel was sealed at 13:12, it left the gate at 13:47.",
                PARCEL_TIMES,
            ),
            (
                "At 13:47 the parcel departed, after it was sealed at 13:12.",
                PARCEL_TIMES,
            ),
        ):
            with self.subTest(text=text):
                for _, value, subject in expected:
                    self.assertTrue(claim_supports(text, value, subject))

    def test_swapped_clauses_cannot_pass_with_both_times_and_originals_present(self):
        for text, expected in (
            ("The witness reported 08:10 and the clerk recorded 09:25.", ARRIVAL_TIMES),
            (
                "The parcel left the gate at 13:12 and was sealed at 13:47.",
                PARCEL_TIMES,
            ),
        ):
            with self.subTest(text=text):
                for _, value, subject in expected:
                    self.assertFalse(claim_supports(text, value, subject))
