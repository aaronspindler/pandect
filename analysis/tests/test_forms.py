"""Visible assumptions fail recoverably before unsupported processing is queued."""

from unittest.mock import patch
from uuid import uuid4

from django.core.files.base import ContentFile
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from analysis.forms import AnalysisForm, ReportForm, source_choices
from analysis.services import process_run, start_run
from analysis.sources import source_id
from evidence.models import (
    DocumentEvidence,
    PersonLocationEvidence,
    TextMessageEvidence,
)
from evidence.tests.test_views import EvidenceViewTestMixin


class AnalysisSelectionFormTests(EvidenceViewTestMixin, TestCase):
    def test_source_picker_stops_before_discarded_types_and_extraction_queries(self):
        records = [self.text() for _ in range(4)]
        with (
            patch("analysis.forms.SOURCE_CHOICE_LIMIT", 3),
            CaptureQueriesContext(connection) as queries,
        ):
            choices = source_choices(self.case, "entities")
        self.assertEqual(
            [key for key, label in choices],
            [f"text:{x.pk}" for x in reversed(records[1:])],
        )
        self.assertFalse(
            any('"evidence_documentevidence"' in query["sql"] for query in queries)
        )
        extraction_queries = [
            query for query in queries if '"analysis_analysisresult"' in query["sql"]
        ]
        self.assertEqual(len(extraction_queries), 2)
        self.assertFalse(any('"body"' in query["sql"] for query in queries))

    def test_picker_keeps_exact_extraction_versions_before_the_bound(self):
        self.text()
        document = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Selected document",
            file=ContentFile(b"Exact text.\n", name="source.txt"),
        )
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "native_text",
            [{"kind": "document", "id": str(document.pk)}],
        )
        process_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "complete", run.error_code)
        with patch("analysis.forms.SOURCE_CHOICE_LIMIT", 3):
            choices = source_choices(self.case, "entities")
        self.assertEqual(len(choices), 3)
        self.assertEqual(choices[1][0], f"document:{document.pk}")
        self.assertEqual(choices[2][0], f"document:{document.pk}:-:{run.result.pk}")

    def setUp(self):
        super().setUp()
        self.point = PersonLocationEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Reported point",
            person=self.person,
            latitude=0,
            longitude=0,
        )
        self.choice = f"person-location:{self.point.pk}"

    def data(self, operation="candidate_visits", **values):
        return {
            "operation": operation,
            "submission_id": str(uuid4()),
            "sources": [self.choice],
        } | values

    def form(self, data):
        return AnalysisForm(data, case=self.case, operation=data["operation"])

    def test_visible_location_bounds_reject_unsupported_values_before_queueing(self):
        for operation, key, value in (
            ("candidate_visits", "dwell_minutes", 1441),
            ("travel_discrepancies", "speed_limit_kmh", 3001),
            ("proximity", "radius_m", 500001),
            ("proximity", "boundary_m", 10001),
        ):
            with self.subTest(operation=operation, key=key):
                form = self.form(self.data(operation, **{key: value}))
                self.assertFalse(form.is_valid())
                self.assertIn(key, form.errors)
        valid = self.form(self.data(dwell_minutes=0, gap_minutes=60))
        self.assertTrue(valid.is_valid(), valid.errors)
        self.assertEqual(valid.parameters()["dwell_minutes"], 0)

    def test_movement_periods_are_explicit_disjoint_and_retained_as_aware_timestamps(
        self,
    ):
        periods = {
            "period_a_start": "2026-10-01T00:00",
            "period_a_end": "2026-10-02T00:00",
            "period_b_start": "2026-10-02T00:00",
            "period_b_end": "2026-10-03T00:00",
        }
        form = self.form(self.data("movement_routines", **periods))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertTrue(form.parameters()["period_a_start"].endswith("+00:00"))
        for broken in (
            {"period_a_end": "2026-10-03T00:00"},
            {"period_b_end": ""},
            {"period_a_start": "2026-10-02T00:00"},
        ):
            invalid = self.form(self.data("movement_routines", **(periods | broken)))
            self.assertFalse(invalid.is_valid())
            self.assertIn("period_a_start", invalid.errors)

    def test_session_adjustments_map_only_selected_sources_to_stable_keys(self):
        messages = [
            TextMessageEvidence.objects.create(
                case=self.case,
                uploaded_by=self.users["owner"],
                title=f"Message {index}",
                sender=self.person,
                recipient=self.person,
                body=f"Message {index}",
            )
            for index in range(2)
        ]
        choices = [f"text-message:{message.pk}" for message in messages]
        data = {
            "operation": "comm_patterns",
            "submission_id": uuid4(),
            "sources": choices[:1],
            "break_before": choices[1:],
            "compare_split_at": "2026-10-01T12:00",
        }
        invalid = AnalysisForm(data, case=self.case, operation="comm_patterns")
        self.assertFalse(invalid.is_valid())
        self.assertIn("break_before", invalid.errors)
        valid = AnalysisForm(
            data | {"sources": choices}, case=self.case, operation="comm_patterns"
        )
        self.assertTrue(valid.is_valid(), valid.errors)
        self.assertEqual(
            valid.parameters()["break_before"],
            [source_id({"kind": "text-message", "id": str(messages[1].pk)})],
        )
        self.assertEqual(
            valid.parameters()["compare_split_at"], "2026-10-01T12:00:00+00:00"
        )

    def test_geographic_lookup_requires_explicit_external_approval(self):
        form = self.form(self.data("geocode_locations"))
        self.assertFalse(form.is_valid())
        self.assertIn("external_processing", form.errors)
        allowed = self.form(self.data("geocode_locations", external_processing=True))
        self.assertTrue(allowed.is_valid(), allowed.errors)

    def test_call_pair_requires_first_participant_and_case_scoped_choices(self):
        form = ReportForm(
            {"state": "active", "other_participant_id": str(self.person.pk)},
            case=self.case,
            report_name="calls",
        )
        self.assertFalse(form.is_valid())
        self.assertIn("participant_id", form.errors)
        invalid = ReportForm(
            {"state": "active", "participant_id": str(uuid4())},
            case=self.case,
            report_name="calls",
        )
        self.assertFalse(invalid.is_valid())
        self.assertIn("participant_id", invalid.errors)
