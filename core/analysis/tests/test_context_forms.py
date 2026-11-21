"""Visible OCR profiles and map-reference settings retain only selected intent."""

from datetime import UTC, datetime
from unittest.mock import patch
from uuid import uuid4

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.utils import timezone

from core.analysis.form_base import source_choices
from core.analysis.forms import analysis_form
from core.analysis.services import start_run
from core.analysis.sources import output
from core.analysis.testing import preview_image_bytes as image_bytes
from core.models import AnalysisResult, AnalysisReview, AnalysisRun
from core.testing import EvidenceViewTestMixin
from geo.models import LocationEvidence
from image.models import ImageEvidence


@override_settings(PANDECT_GEOCODER_LOCAL=True, PANDECT_MAP_CONTEXT_LOCAL=True)
class ContextAndOCRFormTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.image = ImageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic page image",
            file=ContentFile(image_bytes(), name="page.png"),
        )
        self.location = LocationEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic located observation",
            latitude="43.650000",
            longitude="-79.380000",
        )

    def form(self, operation, **values):
        source = (
            f"image:{self.image.pk}"
            if operation == "ocr"
            else f"location:{self.location.pk}"
        )
        return analysis_form(
            {
                "operation": operation,
                "submission_id": str(uuid4()),
                "sources": [source],
                "external_processing": True,
            }
            | values,
            case=self.case,
            operation=operation,
        )

    @override_settings(PANDECT_MAP_CONTEXT_LOCAL=False)
    def test_local_ocr_needs_no_external_approval_and_only_offers_printed_text(self):
        unbound = analysis_form(case=self.case, operation="ocr")
        self.assertEqual(
            list(unbound.fields["mode"].choices), [("printed", "Printed English")]
        )
        self.assertTrue(unbound.fields["external_processing"].widget.is_hidden)
        form = self.form("ocr", mode="printed", external_processing=True)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertFalse(form.cleaned_data["external_processing"])
        self.assertEqual(form.parameters(), {"mode": "printed"})
        form = self.form("ocr", external_processing=False)
        self.assertTrue(form.is_valid(), form.errors)
        for mode in ("handwriting", "mixed", "author-identification"):
            self.assertFalse(self.form("ocr", mode=mode).is_valid())
        context = self.form("geographic_context", external_processing=False)
        self.assertFalse(context.is_valid())
        self.assertIn("external_processing", context.errors)

    def test_map_context_defaults_bounds_and_retained_assumptions(self):
        unbound = analysis_form(case=self.case, operation="geographic_context")
        self.assertEqual(unbound["context_radius_m"].value(), 500)
        self.assertEqual(unbound["distance_m"].value(), 100)
        self.assertEqual(unbound["boundary_m"].value(), 10)
        valid = self.form(
            "geographic_context", context_radius_m=5000, distance_m=100, boundary_m=0
        )
        self.assertTrue(valid.is_valid(), valid.errors)
        self.assertEqual(
            valid.parameters(),
            {"context_radius_m": 5000, "distance_m": 100, "boundary_m": 0},
        )
        for key, value in (
            ("context_radius_m", 0),
            ("context_radius_m", 5001),
            ("context_radius_m", "nan"),
            ("context_radius_m", "inf"),
            ("distance_m", 0),
            ("boundary_m", -1),
            ("boundary_m", 101),
        ):
            with self.subTest(key=key, value=value):
                invalid = self.form("geographic_context", **{key: value})
                self.assertFalse(invalid.is_valid())
                self.assertIn(key, invalid.errors)

    def test_historical_context_uses_utc_and_rejects_future_or_invalid_dates(self):
        now = datetime(2026, 10, 28, 12, tzinfo=UTC)
        with (
            timezone.override("America/Toronto"),
            patch("geo.analysis_forms.timezone.now", return_value=now),
        ):
            for timestamp in ("2026-10-01T12:00", "2026-10-01T08:00:00-04:00"):
                with self.subTest(timestamp=timestamp):
                    form = self.form("geographic_context", context_as_of=timestamp)
                    self.assertIn("UTC", form["context_as_of"].label)
                    self.assertTrue(form.is_valid(), form.errors)
                    self.assertEqual(
                        form.parameters()["context_as_of"],
                        "2026-10-01T12:00:00+00:00",
                    )
            for timestamp in (
                "2026-10-28T12:00:01Z",
                "invalid timestamp",
                "2026-10-01T12:00:00.001Z",
            ):
                invalid = self.form("geographic_context", context_as_of=timestamp)
                self.assertFalse(invalid.is_valid())
                self.assertIn("context_as_of", invalid.errors)
            current = self.form("geographic_context", context_as_of="")
            self.assertTrue(current.is_valid(), current.errors)
            self.assertNotIn("context_as_of", current.parameters())

    def test_movement_period_form_retains_utc_labels_and_boundaries(self):
        with timezone.override("America/Toronto"):
            values = {
                "period_a_start": "2026-03-08T02:30",
                "period_a_end": "2026-03-08T03:00",
                "period_b_start": "2026-03-09T02:30",
                "period_b_end": "2026-03-09T03:00",
            }
            form = self.form("movement_routines", **values)
            self.assertTrue(form.is_valid(), form.errors)
            for field, value in values.items():
                self.assertIn("UTC", form[field].label)
                self.assertEqual(form.parameters()[field], value + ":00+00:00")

    def test_profile_and_context_parameters_are_isolated_to_their_operations(self):
        ocr = self.form(
            "ocr", mode="printed", context_as_of="not a date", context_radius_m="nan"
        )
        self.assertTrue(ocr.is_valid(), ocr.errors)
        self.assertEqual(ocr.parameters(), {"mode": "printed"})
        self.assertNotIn("context_as_of", ocr.fields)
        context = self.form("geographic_context", mode="invalid profile")
        self.assertTrue(context.is_valid(), context.errors)
        self.assertNotIn("mode", context.fields)
        self.assertNotIn("mode", context.parameters())
        local = self.form(
            "spatial_groups",
            external_processing=False,
            context_as_of="not a date",
            context_radius_m="nan",
            mode="invalid profile",
            distance_m=150,
        )
        self.assertTrue(local.is_valid(), local.errors)
        self.assertEqual(local.parameters(), {"distance_m": 150})

    def test_context_picker_accepts_only_currently_accepted_geocode_candidates(self):
        address = LocationEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Address-only observation",
            address="1 Synthetic Street",
        )
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "geocode_locations",
            [{"kind": "location", "id": str(address.pk)}],
        )
        AnalysisRun.objects.filter(pk=run.pk).update(status="complete")
        result = AnalysisResult.objects.create(
            case=self.case,
            run=run,
            data=output(
                "Synthetic geocode candidates",
                items=[
                    {
                        "label": f"Candidate {index}",
                        "source_ids": [run.inputs[0]["id"]],
                        "values": {"latitude": 43.65, "longitude": -79.38},
                    }
                    for index in range(2)
                ],
            ),
        )
        AnalysisReview.objects.create(
            case=self.case,
            result=result,
            reviewed_by=self.users["owner"],
            item_index=0,
            decision="accepted",
        )
        prefix = f"location:{address.pk}:-:{result.pk}"
        accepted = f"{prefix}:0"
        choices = dict(source_choices(self.case, "geographic_context"))
        self.assertIn(accepted, choices)
        self.assertNotIn(f"{prefix}:1", choices)
        selected = self.form("geographic_context", sources=[accepted])
        self.assertTrue(selected.is_valid(), selected.errors)
        AnalysisReview.objects.create(
            case=self.case,
            result=result,
            reviewed_by=self.users["owner"],
            item_index=0,
            decision="rejected",
        )
        revoked = self.form("geographic_context", sources=[accepted])
        self.assertFalse(revoked.is_valid())
        self.assertIn("sources", revoked.errors)

    def test_context_form_rejects_source_choices_from_another_case(self):
        foreign = LocationEvidence.objects.create(
            case=self.other_case,
            uploaded_by=self.users["outsider"],
            title="Other case position",
            latitude=1,
            longitude=2,
        )
        form = self.form("geographic_context", sources=[f"location:{foreign.pk}"])
        self.assertFalse(form.is_valid())
        self.assertIn("sources", form.errors)
