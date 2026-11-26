"""Failure diagnostics retain code locations while excluding private content."""

import logging
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import SimpleTestCase, TestCase

from core.analysis.diagnostics import log_analysis_failure
from core.analysis.errors import AnalysisError
from core.analysis.services import start_run
from core.analysis.worker import process_run
from core.models import AnalysisResult
from core.testing import EvidenceViewTestMixin
from text.models import TextEvidence

PRIVATE_TEXT = "synthetic-private-body-and-api-token"


def raise_private_failure(error):
    private_local = PRIVATE_TEXT
    try:
        raise ValueError(private_local)
    except ValueError as cause:
        raise error from cause


class FailureMetadataTests(SimpleTestCase):
    def test_validation_diagnostics_exclude_exception_details_and_locals(self):
        run = SimpleNamespace(pk=uuid4(), operation="content_events")
        error = ValidationError(
            PRIVATE_TEXT,
            code="model_quote_mismatch",
            params={"quote": PRIVATE_TEXT},
        )
        with self.assertLogs("core.analysis.failures", level="ERROR") as logs:
            try:
                raise_private_failure(error)
            except ValidationError as failure:
                log_analysis_failure(run, failure, "processing_failed", "analysis")
        record = logs.records[0]
        rendered = logging.Formatter().format(record)
        self.assertIn(str(run.pk), rendered)
        self.assertIn("operation=content_events", rendered)
        self.assertIn("exception_type=ValidationError", rendered)
        self.assertIn("validation_code=model_quote_mismatch", rendered)
        self.assertIn("test_error_logging:raise_private_failure:", rendered)
        self.assertNotIn(PRIVATE_TEXT, rendered)
        self.assertNotIn(PRIVATE_TEXT, repr(record.__dict__))
        self.assertIsNone(record.exc_info)
        self.assertIsNone(record.exc_text)
        self.assertIsNone(record.stack_info)
        self.assertTrue(all(isinstance(value, str) for value in record.args))

    def test_unknown_metadata_is_not_used_as_log_content(self):
        run = SimpleNamespace(pk=uuid4(), operation=PRIVATE_TEXT)
        error = ValidationError(PRIVATE_TEXT, code=PRIVATE_TEXT)
        with self.assertLogs("core.analysis.failures", level="ERROR") as logs:
            log_analysis_failure(run, error, PRIVATE_TEXT, "analysis")
        record = logs.records[0]
        self.assertIn("operation=unknown", record.getMessage())
        self.assertIn("error_code=processing_failed", record.getMessage())
        self.assertIn("validation_code=none", record.getMessage())
        self.assertNotIn(PRIVATE_TEXT, repr(record.__dict__))

    def test_traceback_metadata_is_bounded_without_external_paths(self):
        run = SimpleNamespace(pk=uuid4(), operation="entities")

        def recurse(depth):
            if depth:
                return recurse(depth - 1)
            raise RuntimeError(PRIVATE_TEXT)

        with self.assertLogs("core.analysis.failures", level="ERROR") as logs:
            try:
                recurse(20)
            except RuntimeError as failure:
                log_analysis_failure(run, failure, "processing_failed", "analysis")
        record = logs.records[0]
        locations = record.args[-1].split(",")
        self.assertEqual(len(locations), 8)
        self.assertTrue(all("test_error_logging:recurse:" in x for x in locations))
        self.assertNotIn(PRIVATE_TEXT, repr(record.__dict__))
        self.assertNotIn("/Users/", record.getMessage())


class ProcessingFailureLoggingTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.source = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic failure diagnostic source",
            body=PRIVATE_TEXT,
        )

    def start(self):
        return start_run(
            self.users["owner"],
            self.case.pk,
            "entities",
            [{"kind": "text", "id": str(self.source.pk)}],
        )[0]

    def test_operation_failure_logs_safe_category_and_preserves_failed_history(self):
        run = self.start()

        def operation(sources, parameters):
            raise ValidationError(PRIVATE_TEXT, code="model_citation_source")

        with (
            patch(
                "core.analysis.worker.get_operation",
                return_value=SimpleNamespace(
                    function=operation, version=run.method_version
                ),
            ),
            self.assertLogs("core.analysis.failures", level="ERROR") as logs,
        ):
            self.assertEqual(process_run(str(run.pk)), "model_citation_source")
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error_code, "model_citation_source")
        self.assertIsNotNone(run.finished_at)
        self.assertFalse(AnalysisResult.objects.filter(run=run).exists())
        self.assertIn("stage=analysis", logs.records[0].getMessage())
        self.assertIn(
            "validation_code=model_citation_source", logs.records[0].getMessage()
        )
        self.assertNotIn(str(self.source.pk), repr(logs.records[0].__dict__))
        self.assertNotIn(PRIVATE_TEXT, repr(logs.records[0].__dict__))
        with self.assertNoLogs("core.analysis.failures", level="ERROR"):
            self.assertEqual(process_run(str(run.pk)), "failed")

    def test_known_access_and_processing_errors_log_safe_codes(self):
        for error, code in (
            (PermissionDenied(PRIVATE_TEXT), "access_removed"),
            (AnalysisError("gateway_invalid_response"), "gateway_invalid_response"),
        ):
            with self.subTest(code=code):
                run = self.start()
                with (
                    patch("core.analysis.worker._authorize_run", side_effect=error),
                    self.assertLogs("core.analysis.failures", level="ERROR") as logs,
                ):
                    self.assertEqual(process_run(str(run.pk)), code)
                self.assertIn(f"error_code={code}", logs.records[0].getMessage())
                self.assertIn("stage=authorization", logs.records[0].getMessage())
                self.assertNotIn(PRIVATE_TEXT, repr(logs.records[0].__dict__))

    def test_publication_exception_remains_atomic_and_logs_its_stage(self):
        run = self.start()
        create = AnalysisResult.objects.create

        def fail_after_insert(**values):
            create(**values)
            raise RuntimeError(PRIVATE_TEXT)

        with (
            patch(
                "core.analysis.worker.AnalysisResult.objects.create",
                side_effect=fail_after_insert,
            ),
            self.assertLogs("core.analysis.failures", level="ERROR") as logs,
        ):
            self.assertEqual(process_run(str(run.pk)), "processing_failed")
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        self.assertFalse(AnalysisResult.objects.filter(run=run).exists())
        self.assertIn("stage=publication", logs.records[0].getMessage())
        self.assertIn("exception_type=RuntimeError", logs.records[0].getMessage())
        self.assertNotIn(PRIVATE_TEXT, repr(logs.records[0].__dict__))

    def test_broken_log_sink_does_not_leave_running_work(self):
        run = self.start()
        with (
            patch(
                "core.analysis.worker._authorize_run",
                side_effect=RuntimeError(PRIVATE_TEXT),
            ),
            patch(
                "core.analysis.diagnostics.logger.error",
                side_effect=OSError(PRIVATE_TEXT),
            ),
        ):
            self.assertEqual(process_run(str(run.pk)), "processing_failed")
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        self.assertIsNotNone(run.finished_at)
        self.assertFalse(AnalysisResult.objects.filter(run=run).exists())
