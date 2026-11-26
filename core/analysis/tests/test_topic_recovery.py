"""Topic-stage failures retain keywords without bypassing publication checks."""

import json
from unittest.mock import Mock, patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings

from cases.models import CaseMembership
from core.analysis.errors import AnalysisError, error_message
from core.analysis.services import start_run
from core.analysis.testing import external_grant_kwargs
from core.analysis.tests.test_gateway import CONFIG
from core.analysis.worker import process_run
from core.models import AnalysisResult, AnalysisRun
from core.testing import EvidenceViewTestMixin
from text.models import TextEvidence


@override_settings(PANDECT_AI_GATEWAY=CONFIG)
class TopicRecoveryTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.note = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic plan",
            body="Harbour delivery is unconfirmed. Harbour delivery remains proposed.",
        )

    def start(self, *, backend="gateway", role="owner", previous_run=None):
        return start_run(
            self.users[role],
            self.case.pk,
            "text_topics",
            [{"kind": "text", "id": str(self.note.pk)}],
            parameters={"backend": backend, "include_summary": True},
            previous_run=previous_run,
            **(
                external_grant_kwargs(self.case, "text_topics")
                if backend == "gateway"
                else {}
            ),
        )[0]

    def process(self, run, *, response=None, error=None):
        client = Mock(usage={"request_count": 1})
        client.infer.return_value = response
        client.infer.side_effect = error
        with patch("core.analysis.gateway.GatewayClient", return_value=client):
            process_run(run.pk)
        run.refresh_from_db()
        return run.status

    def assert_keywords_only(self, run):
        self.assertEqual(run.status, AnalysisRun.Status.PARTIAL, run.error_code)
        self.assertEqual(run.error_code, "")
        self.assertTrue(run.result.data["items"])
        self.assertTrue(
            all("occurrences" in item["values"] for item in run.result.data["items"])
        )
        for item in run.result.data["items"]:
            for anchor in item["anchors"]:
                self.assertEqual(anchor["source_id"], run.inputs[0]["id"])
                self.assertEqual(
                    self.note.body[anchor["start"] : anchor["end"]].casefold(),
                    item["label"],
                )

    def test_invalid_topic_structure_and_quotes_publish_only_keywords(self):
        for response in (
            {"items": "PRIVATE UNVALIDATED RESPONSE"},
            {
                "items": [
                    {
                        "kind": "Topic",
                        "text": "PRIVATE UNVALIDATED RESPONSE",
                        "citations": [
                            {"source_id": "selected-source", "quote": "invented"}
                        ],
                    }
                ]
            },
        ):
            with self.subTest(response=response):
                run = self.start()
                if isinstance(response["items"], list):
                    response["items"][0]["citations"][0]["source_id"] = run.inputs[0][
                        "id"
                    ]
                self.process(run, response=response)
                self.assert_keywords_only(run)
                self.assertNotIn(
                    "PRIVATE UNVALIDATED RESPONSE", json.dumps(run.result.data)
                )
                self.assertEqual(
                    run.result.provenance["execution"]["backend"], "gateway"
                )

    def test_private_validation_errors_are_not_retained(self):
        run = self.start()
        self.process(run, error=ValidationError("PRIVATE MODEL ERROR"))
        self.assert_keywords_only(run)
        self.assertNotIn("PRIVATE MODEL ERROR", json.dumps(run.result.data))

    def test_gateway_outages_keep_keywords_and_safe_recovery(self):
        run = self.start()
        self.process(run, error=AnalysisError("gateway_unavailable"))
        self.assert_keywords_only(run)
        self.assertIn(
            error_message("gateway_unavailable"), " ".join(run.result.data["warnings"])
        )

    def test_security_and_source_failures_do_not_publish_partial_results(self):
        for error, code in (
            (AnalysisError("access_removed"), "access_removed"),
            (AnalysisError("source_changed"), "source_changed"),
            (AnalysisError("execution_changed"), "execution_changed"),
            (PermissionDenied("PRIVATE AUTHORIZATION ERROR"), "access_removed"),
        ):
            with self.subTest(code=code, exception=type(error)):
                run = self.start()
                self.process(run, error=error)
                self.assertEqual(run.status, AnalysisRun.Status.FAILED)
                self.assertEqual(run.error_code, code)
                self.assertFalse(AnalysisResult.objects.filter(run=run).exists())

    def test_revocation_during_topic_failure_withholds_keywords(self):
        run = self.start(role="editor")

        def revoke(*args):
            CaseMembership.objects.filter(
                case=self.case, user=self.users["editor"]
            ).delete()
            raise ValidationError("PRIVATE MODEL ERROR")

        self.process(run, error=revoke)
        self.assertEqual(run.status, AnalysisRun.Status.FAILED)
        self.assertEqual(run.error_code, "access_removed")
        self.assertFalse(AnalysisResult.objects.filter(run=run).exists())

    def test_retry_preserves_failed_and_partial_history(self):
        failed = self.start()
        self.process(failed, error=AnalysisError("source_changed"))
        partial = self.start(previous_run=failed)
        self.process(partial, error=ValidationError("PRIVATE MODEL ERROR"))
        self.assert_keywords_only(partial)
        retained_result = partial.result

        retried = self.start(previous_run=partial)
        self.process(retried, response={"items": []})

        self.assertEqual(
            retried.status, AnalysisRun.Status.COMPLETE, retried.error_code
        )
        self.assertEqual(retried.previous_run_id, partial.pk)
        self.assertEqual(partial.previous_run_id, failed.pk)
        failed.refresh_from_db()
        partial.refresh_from_db()
        self.assertEqual(failed.status, AnalysisRun.Status.FAILED)
        self.assertEqual(partial.status, AnalysisRun.Status.PARTIAL)
        self.assertEqual(partial.result.pk, retained_result.pk)
        self.assertEqual(partial.result.data, retained_result.data)
