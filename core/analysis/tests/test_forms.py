"""Visible assumptions fail recoverably before unsupported processing is queued."""

from unittest.mock import patch
from uuid import uuid4

from django.core.files.base import ContentFile
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from core.analysis.form_base import source_choices
from core.analysis.forms import analysis_form, report_form
from core.analysis.services import process_run, start_run
from core.analysis.sources import source_id
from core.models import AnalysisResult, AnalysisReview, AnalysisRun
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from geo.models import LocationEvidence, PersonLocationEvidence
from phone.models import TextMessageEvidence


class AnalysisSelectionFormTests(EvidenceViewTestMixin, TestCase):
    def test_source_picker_continues_without_loading_original_bodies(self):
        records = [self.text() for _ in range(4)]
        with (
            patch("core.analysis.form_base.SOURCE_PAGE_SIZE", 3),
            CaptureQueriesContext(connection) as queries,
        ):
            choices = source_choices(self.case, "entities")
        self.assertEqual(
            [key for key, label in choices],
            [f"text:{x.pk}" for x in reversed(records[1:])],
        )
        self.assertTrue(choices.has_next())
        with patch("core.analysis.form_base.SOURCE_PAGE_SIZE", 3):
            second = source_choices(self.case, "entities", page=2, cursor=choices.after)
        self.assertEqual(second[0][0], f"text:{records[0].pk}")
        extraction_queries = [
            query for query in queries if '"core_analysisresult"' in query["sql"]
        ]
        self.assertEqual(extraction_queries, [])
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
        with patch("core.analysis.form_base.SOURCE_PAGE_SIZE", 3):
            choices = source_choices(self.case, "entities")
        self.assertEqual(len(choices), 3)
        self.assertEqual(choices[1][0], f"document:{document.pk}")
        self.assertEqual(choices[2][0], f"document:{document.pk}:-:{run.result.pk}")

    def test_picker_reads_geocode_candidates_and_latest_reviews_once_per_result(self):
        originals = [
            LocationEvidence.objects.create(
                case=self.case,
                uploaded_by=self.users["owner"],
                title=f"Address {index}",
                address=f"{index} Synthetic Street",
            )
            for index in (1, 2)
        ]
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "geocode_locations",
            [{"kind": "location", "id": str(item.pk)} for item in originals],
            external_processing=True,
        )
        AnalysisRun.objects.filter(pk=run.pk).update(status="complete")
        result = AnalysisResult.objects.create(
            case=self.case,
            run=run,
            data={
                "items": [
                    {
                        "label": f"Candidate {index}",
                        "source_ids": [run.inputs[index // 2]["id"]],
                        "values": {"latitude": 1, "longitude": 2},
                    }
                    for index in range(4)
                ]
            },
        )
        for index, decision in (
            (0, "accepted"),
            (1, "accepted"),
            (2, "accepted"),
            (1, "rejected"),
            (3, "unresolved"),
        ):
            AnalysisReview.objects.create(
                case=self.case,
                result=result,
                reviewed_by=self.users["owner"],
                item_index=index,
                decision=decision,
            )
        with CaptureQueriesContext(connection) as queries:
            choices = source_choices(self.case, "location_quality")
        derived = [value for value, _ in choices if str(result.pk) in value]
        self.assertCountEqual(
            derived,
            [
                f"location:{item.pk}:-:{result.pk}:{index}"
                for item, index in zip(originals, (0, 2), strict=True)
            ],
        )
        self.assertEqual(
            sum(
                query["sql"].split(" FROM ", 1)[-1].startswith('"core_analysisreview"')
                for query in queries
            ),
            1,
        )
        self.assertEqual(
            sum('"core_analysisresult"."data"' in query["sql"] for query in queries),
            1,
        )

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
        return analysis_form(data, case=self.case, operation=data["operation"])

    def test_inactive_fields_do_not_validate_or_enter_retained_parameters(self):
        note = self.text()
        form = self.form(
            self.data(
                "entities",
                sources=[f"text:{note.pk}"],
                period_a_start="not a date",
                polygon="not a polygon",
                semantic_links=True,
                reverse=True,
            )
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.parameters(), {})
        self.assertNotIn("period_a_start", form.fields)
        self.assertNotIn("me", form.fields)
        self.assertEqual(len(form.fields), 4)

    def test_polygon_is_parsed_once_and_retains_coordinate_zero(self):
        form = self.form(self.data("proximity", polygon="0, 0\n0, 1\n1, 1"))
        self.assertTrue(form.is_valid(), form.errors)
        expected = [[0, 0], [0, 1], [1, 1]]
        self.assertEqual(form.cleaned_data["polygon"], expected)
        self.assertEqual(form.parameters()["polygon"], expected)
        invalid = self.form(self.data("proximity", polygon="0, 0\n1, 1"))
        self.assertFalse(invalid.is_valid())
        self.assertIn("polygon", invalid.errors)

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

    def test_location_assumptions_match_the_operation_parameters(self):
        for operation in ("location_quality", "travel_discrepancies"):
            with self.subTest(operation=operation):
                form = self.form(self.data(operation, distance_m=250))
                self.assertTrue(form.is_valid(), form.errors)
                self.assertEqual(form.parameters()["distance_m"], 250)
                self.assertEqual(
                    form.fields["distance_m"].label,
                    "Coordinate discrepancy threshold (metres)",
                )
        quality = self.form(self.data("location_quality", gap_minutes="unused"))
        self.assertTrue(quality.is_valid(), quality.errors)
        self.assertNotIn("gap_minutes", quality.fields)
        conflict = self.form(
            self.data(
                "location_conflicts",
                event_label="Reported delivery",
                distance_m="unused",
                time_window_minutes="unused",
            )
        )
        self.assertTrue(conflict.is_valid(), conflict.errors)
        self.assertEqual(conflict.parameters(), {"event_label": "Reported delivery"})

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
        invalid = analysis_form(data, case=self.case, operation="comm_patterns")
        self.assertFalse(invalid.is_valid())
        self.assertIn("break_before", invalid.errors)
        valid = analysis_form(
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

    @override_settings(PANDECT_GEOCODER_LOCAL=False)
    def test_geographic_lookup_requires_explicit_external_approval(self):
        form = self.form(self.data("geocode_locations"))
        self.assertFalse(form.is_valid())
        self.assertIn("external_processing", form.errors)
        review = analysis_form(case=self.case, operation="geocode_locations")
        allowed = self.form(
            self.data(
                "geocode_locations",
                external_processing=True,
                submission_id=review["submission_id"].value(),
                execution_confirmation=review["execution_confirmation"].value(),
            )
        )
        self.assertTrue(allowed.is_valid(), allowed.errors)

    def test_call_pair_requires_first_participant_and_case_scoped_choices(self):
        form = report_form(
            {"state": "active", "other_participant_id": str(self.person.pk)},
            case=self.case,
            report_name="calls",
        )
        self.assertFalse(form.is_valid())
        self.assertIn("participant_id", form.errors)
        invalid = report_form(
            {"state": "active", "participant_id": str(uuid4())},
            case=self.case,
            report_name="calls",
        )
        self.assertFalse(invalid.is_valid())
        self.assertIn("participant_id", invalid.errors)

    def test_reports_only_load_choices_for_applicable_filters(self):
        for report_name, count, filters in (
            ("calls", 1, {"participant_id", "other_participant_id"}),
            ("vehicle-coverage", 1, {"vehicle_id"}),
            ("vehicle-speed", 1, {"vehicle_id"}),
            ("duplicates", 0, set()),
        ):
            with self.subTest(report=report_name), self.assertNumQueries(count):
                form = report_form(
                    {"state": "active"}, case=self.case, report_name=report_name
                )
                self.assertTrue(form.is_valid(), form.errors)
                self.assertEqual(set(form.fields), {"state", "start", "end"} | filters)
