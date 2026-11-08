"""Imported account labels map only to currently available case people."""

from uuid import uuid4

from django.test import TestCase

from core.analysis.forms import analysis_form
from core.testing import EvidenceTestMixin
from document.models import DocumentEvidence
from people.models import Person


class CommunicationAnalysisFormTests(EvidenceTestMixin, TestCase):
    def test_account_mapping_serialization_and_invalid_people_or_labels(self):
        original = self.create_evidence(DocumentEvidence)
        other = Person.objects.create(case=self.case, first_name="Other participant")
        outside = Person.objects.create(case=self.other_case, first_name="Outside")
        data = {
            "operation": "conversation_import",
            "submission_id": uuid4(),
            "sources": [f"document:{original.pk}"],
            "timezone": "America/Toronto",
            "me_label": "Account A",
            "them_label": "Account B",
            "me": str(self.person.pk),
            "them": str(other.pk),
        }
        form = analysis_form(data, case=self.case, operation="conversation_import")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.assumptions, [])
        self.assertEqual(
            form.parameters(),
            {
                "timezone": "America/Toronto",
                "sender_map": {
                    "Account A": str(self.person.pk),
                    "Account B": str(other.pk),
                },
                "recipient_map": {
                    "Account A": str(other.pk),
                    "Account B": str(self.person.pk),
                },
            },
        )
        for override, field in (
            ({"them": str(outside.pk)}, "them"),
            ({"them_label": "Account A"}, "them_label"),
        ):
            with self.subTest(field=field):
                invalid = analysis_form(
                    data | override, case=self.case, operation="conversation_import"
                )
                self.assertFalse(invalid.is_valid())
                self.assertIn(field, invalid.errors)
