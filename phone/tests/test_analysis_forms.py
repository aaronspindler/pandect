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
            "participants-TOTAL_FORMS": "2",
            "participants-INITIAL_FORMS": "2",
            "participants-0-source_label": "Account A",
            "participants-1-source_label": "Account B",
            "participants-0-person": str(self.person.pk),
            "participants-1-person": str(other.pk),
            "participants-0-fallback_recipient": str(other.pk),
            "participants-1-fallback_recipient": str(self.person.pk),
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
            ({"participants-1-person": str(outside.pk)}, "person"),
            (
                {"participants-1-fallback_recipient": str(outside.pk)},
                "fallback_recipient",
            ),
            ({"participants-1-source_label": "Account A"}, "source_label"),
        ):
            with self.subTest(field=field):
                invalid = analysis_form(
                    data | override, case=self.case, operation="conversation_import"
                )
                self.assertFalse(invalid.is_valid())
                self.assertIn("__all__", invalid.errors)

    def test_three_labels_leave_recipient_omissions_unresolved(self):
        original = self.create_evidence(DocumentEvidence)
        people = [self.person] + [
            Person.objects.create(case=self.case, first_name=f"Person {i}")
            for i in (2, 3)
        ]
        data = {
            "operation": "conversation_import",
            "submission_id": uuid4(),
            "sources": [f"document:{original.pk}"],
            "timezone": "UTC",
            "participants-TOTAL_FORMS": "3",
            "participants-INITIAL_FORMS": "2",
        }
        for index, person in enumerate(people):
            data |= {
                f"participants-{index}-source_label": f"Account {index}",
                f"participants-{index}-person": str(person.pk),
            }
        form = analysis_form(data, case=self.case, operation="conversation_import")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(len(form.parameters()["sender_map"]), 3)
        self.assertEqual(form.parameters()["recipient_map"], {})

    def test_mapping_edits_preserve_sources_and_values_without_validation(self):
        original = self.create_evidence(DocumentEvidence)
        data = {
            "operation": "conversation_import",
            "submission_id": str(uuid4()),
            "sources": [f"document:{original.pk}"],
            "timezone": "America/Toronto",
            "participants-TOTAL_FORMS": "2",
            "participants-INITIAL_FORMS": "2",
            "participants-0-source_label": "Account A",
            "participants-0-person": str(self.person.pk),
            "participants-1-source_label": "Account B",
            "mapping_action": "add",
        }
        added = analysis_form(data, case=self.case, operation="conversation_import")
        self.assertTrue(added.is_mapping_edit)
        self.assertFalse(added.is_bound)
        self.assertFalse(added.errors)
        self.assertEqual(len(added.mapping_formset), 3)
        self.assertEqual(added["sources"].value(), data["sources"])
        self.assertEqual(added["timezone"].value(), "America/Toronto")
        self.assertEqual(
            added.mapping_formset.forms[0]["person"].value(), str(self.person.pk)
        )
        removed = analysis_form(
            data | {"mapping_action": "remove:0"},
            case=self.case,
            operation="conversation_import",
        )
        self.assertEqual(len(removed.mapping_formset), 1)
        self.assertEqual(
            removed.mapping_formset.forms[0]["source_label"].value(), "Account B"
        )

    def test_forged_management_count_is_bounded(self):
        form = analysis_form(
            {
                "operation": "conversation_import",
                "participants-TOTAL_FORMS": "1000000",
                "participants-INITIAL_FORMS": "0",
            },
            case=self.case,
            operation="conversation_import",
        )
        self.assertFalse(form.is_valid())
        self.assertEqual(len(form.mapping_formset), 100)
        self.assertIn("at most 100", str(form.mapping_formset.non_form_errors()))
