"""Source browsing seeks database cursors rather than counting the whole case."""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from core.analysis.form_base import source_choices
from core.analysis.forms import analysis_form
from core.models import AnalysisResult, AnalysisRun
from core.tests.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from geo.models import LocationEvidence
from phone.models import CallEvidenceAttachment, PhoneCallEvidence


class SourceCursorTests(EvidenceViewTestMixin, TestCase):
    def test_large_location_case_loads_only_one_page_and_lookahead(self):
        LocationEvidence.objects.bulk_create(
            [
                LocationEvidence(
                    case=self.case,
                    uploaded_by=self.users["owner"],
                    title=f"Address {number}",
                    address="Synthetic address",
                    sha256="0" * 64,
                )
                for number in range(1003)
            ]
        )
        with CaptureQueriesContext(connection) as queries:
            first = source_choices(self.case, "location_quality")
        self.assertEqual(len(first), 25)
        self.assertTrue(first.has_next())
        self.assertLess(len(queries), 35)
        with CaptureQueriesContext(connection) as queries:
            second = source_choices(
                self.case, "location_quality", page=2, cursor=first.after
            )
        self.assertEqual(len(second), 25)
        self.assertLess(len(queries), 35)
        self.assertTrue(set(dict(first)).isdisjoint(dict(second)))
        with CaptureQueriesContext(connection) as queries:
            previous = source_choices(
                self.case, "location_quality", cursor=second.before, reverse=True
            )
        self.assertEqual(list(previous), list(first))
        self.assertLess(len(queries), 35)

    def test_every_retained_version_remains_reachable_in_both_directions(self):
        document = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Retained source",
            file=SimpleUploadedFile("source.txt", b"Synthetic retained text."),
        )
        for _ in range(34):
            run = AnalysisRun.objects.create(
                case=self.case,
                requested_by=self.users["owner"],
                operation="native_text",
                status="complete",
                inputs=[
                    {
                        "kind": "document",
                        "evidence_id": str(document.pk),
                        "id": "synthetic-source",
                    }
                ],
            )
            AnalysisResult.objects.create(case=self.case, run=run, data={"items": []})
        first = source_choices(self.case, "entities", evidence=("document", document))
        second = source_choices(
            self.case,
            "entities",
            evidence=("document", document),
            page=2,
            cursor=first.after,
        )
        self.assertEqual(len(first), 25)
        self.assertEqual(len(second), 10)
        self.assertFalse(second.has_next())
        self.assertEqual(len(set(dict(first)) | set(dict(second))), 35)
        previous = source_choices(
            self.case,
            "entities",
            evidence=("document", document),
            cursor=second.before,
            reverse=True,
        )
        self.assertEqual(list(previous), list(first))
        selected = second[-1][0]
        form = analysis_form(
            {"operation": "entities", "sources": [selected]},
            case=self.case,
            operation="entities",
            evidence=("document", document),
        )
        self.assertIn(selected, dict(form.fields["sources"].choices))
        self.assertEqual(form.retained_sources[0][0], selected)

    def test_empty_calls_are_skipped_in_sql_for_audio_source_choices(self):
        PhoneCallEvidence.objects.bulk_create(
            [
                PhoneCallEvidence(
                    case=self.case,
                    uploaded_by=self.users["owner"],
                    sender=self.person,
                    recipient=self.person,
                    title="Metadata-only call",
                    sha256="0" * 64,
                )
                for _ in range(1003)
            ]
        )
        call = PhoneCallEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            sender=self.person,
            recipient=self.person,
            title="Call with audio",
        )
        attachment = CallEvidenceAttachment.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            phone_call=call,
            file=SimpleUploadedFile("audio.wav", b"Synthetic retained bytes."),
        )
        with CaptureQueriesContext(connection) as queries:
            choices = source_choices(self.case, "audio_metadata")
        self.assertEqual(
            [value for value, _ in choices], [f"phone-call:{call.pk}:{attachment.pk}"]
        )
        self.assertLess(len(queries), 10)

    def test_unreviewed_geocode_history_is_skipped_in_the_database(self):
        location = LocationEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Address",
            address="Synthetic address",
        )
        runs = AnalysisRun.objects.bulk_create(
            [
                AnalysisRun(
                    case=self.case,
                    requested_by=self.users["owner"],
                    operation="geocode_locations",
                    status="complete",
                    inputs=[
                        {
                            "kind": "location",
                            "evidence_id": str(location.pk),
                            "id": "source",
                        }
                    ],
                )
                for _ in range(503)
            ]
        )
        AnalysisResult.objects.bulk_create(
            [
                AnalysisResult(case=self.case, run=run, data={"items": []})
                for run in runs
            ]
        )
        with CaptureQueriesContext(connection) as queries:
            choices = source_choices(self.case, "location_quality")
        self.assertEqual(len(choices), 1)
        self.assertLess(len(queries), 10)

    def test_call_addition_cursors_cross_kinds_and_preserve_previous_page(self):
        self.text()
        call = PhoneCallEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            sender=self.person,
            recipient=self.person,
            title="Call transcripts",
        )
        additions = [
            CallEvidenceAttachment.objects.create(
                case=self.case,
                uploaded_by=self.users["owner"],
                phone_call=call,
                transcript=f"Synthetic transcript {number}",
            )
            for number in range(35)
        ]
        first = source_choices(self.case, "entities")
        second = source_choices(self.case, "entities", cursor=first.after, page=2)
        self.assertEqual(len(first) + len(second), 37)
        self.assertFalse(second.has_next())
        all_values = set(dict(first)) | set(dict(second))
        self.assertTrue(
            {f"phone-call:{call.pk}:{addition.pk}" for addition in additions}
            <= all_values
        )
        previous = source_choices(
            self.case, "entities", cursor=second.before, reverse=True
        )
        self.assertEqual(list(previous), list(first))
