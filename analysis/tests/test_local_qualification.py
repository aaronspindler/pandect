"""A real-operation qualification harness without case or evidence mutations."""

import json
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from analysis.errors import AnalysisError
from analysis.management.commands.evaluate_local_analysis import synthetic_sources
from analysis.tests.test_local_inference import EXECUTION, wav_bytes

CONFIG = {
    "text": EXECUTION,
    "transcription": EXECUTION
    | {
        "model": "synthetic-whisper",
        "runtime": "whisper.cpp-synthetic-revision",
    },
    "token": "synthetic-secret",
    **{
        key: EXECUTION[key]
        for key in (
            "timeout",
            "max_request_bytes",
            "max_response_bytes",
            "max_output_tokens",
        )
    },
}


@override_settings(PANDECT_LOCAL_AI=CONFIG)
class LocalQualificationTests(SimpleTestCase):
    def test_transcription_selection_skips_all_text_models(self):
        output = StringIO()
        with (
            patch(
                "analysis.management.commands.evaluate_local_analysis.LocalClient"
            ) as client,
            patch(
                "analysis.management.commands.evaluate_local_analysis.Command._audio",
                return_value={"items": [], "outcome": "empty"},
            ) as audio,
        ):
            client.return_value.usage = {}
            call_command(
                "evaluate_local_analysis",
                methods=["transcription"],
                audio="/synthetic.wav",
                stdout=output,
            )
        report = json.loads(output.getvalue())
        self.assertEqual(report["summary"]["completed"], ["transcription"])
        self.assertEqual(len(report["summary"]["not_run"]), 4)
        self.assertEqual(client.call_count, 1)
        client.return_value.infer.assert_not_called()
        audio.assert_called_once_with("/synthetic.wav", client.return_value)

    def test_transcription_selection_requires_audio_before_dispatch(self):
        output = StringIO()
        with patch(
            "analysis.management.commands.evaluate_local_analysis.LocalClient"
        ) as client:
            with self.assertRaisesMessage(CommandError, "requires --audio"):
                call_command(
                    "evaluate_local_analysis", methods=["transcription"], stdout=output
                )
        client.assert_not_called()
        self.assertEqual(output.getvalue(), "")

    def test_all_four_text_operations_run_without_database_or_gateway(self):
        output = StringIO()
        with (
            patch(
                "analysis.management.commands.evaluate_local_analysis.LocalClient"
            ) as client,
            patch("analysis.gateway.GatewayClient.request") as gateway,
        ):
            client.return_value.infer.return_value = {"items": []}
            client.return_value.usage = {"request_count": 1, "output_tokens": 5}
            call_command("evaluate_local_analysis", stdout=output)
        report = json.loads(output.getvalue())
        self.assertEqual(len(report["summary"]["completed"]), 4)
        self.assertEqual(report["summary"]["failed"], [])
        self.assertEqual(report["summary"]["not_run"], ["transcription"])
        self.assertEqual(client.return_value.infer.call_count, 4)
        self.assertEqual(len(report["expected_distinctions"]), 5)
        self.assertEqual(len(report["sources"]), 6)
        self.assertIn("cancel", json.dumps(report["sources"]).lower())
        self.assertNotIn("synthetic-secret", output.getvalue())
        for result in report["methods"].values():
            if result["status"] == "completed":
                self.assertIn("output", result)
                self.assertEqual(result["execution"]["backend"], "local")
                self.assertGreaterEqual(result["elapsed_seconds"], 0)
        gateway.assert_not_called()

    def test_unavailable_runtime_reports_each_failure_and_continues(self):
        output = StringIO()
        with (
            patch(
                "analysis.management.commands.evaluate_local_analysis.LocalClient"
            ) as client,
            patch("analysis.gateway.GatewayClient.request") as gateway,
        ):
            client.side_effect = AnalysisError("local_ai_unavailable")
            with self.assertRaisesMessage(CommandError, "qualification failures"):
                call_command("evaluate_local_analysis", stdout=output)
        report = json.loads(output.getvalue())
        self.assertEqual(len(report["summary"]["failed"]), 4)
        for name in report["summary"]["failed"]:
            self.assertEqual(
                report["methods"][name]["error_code"], "local_ai_unavailable"
            )
            self.assertIsNone(report["methods"][name]["usage"])
        gateway.assert_not_called()

    def test_fabricated_quotation_is_reported_as_rejected(self):
        output = StringIO()
        sources = synthetic_sources()
        with patch(
            "analysis.management.commands.evaluate_local_analysis.LocalClient"
        ) as client:
            client.return_value.infer.return_value = {
                "items": [
                    {
                        "kind": "Claim",
                        "text": "Unsupported meaning",
                        "citations": [
                            {
                                "source_id": sources[0].id,
                                "quote": "This quotation is absent from the source.",
                            }
                        ],
                    }
                ]
            }
            client.return_value.usage = {"request_count": 1}
            with self.assertRaisesMessage(CommandError, "qualification failures"):
                call_command("evaluate_local_analysis", stdout=output)
        report = json.loads(output.getvalue())
        self.assertEqual(len(report["summary"]["failed"]), 4)
        for name in report["summary"]["failed"]:
            self.assertEqual(
                report["methods"][name]["error_code"], "citation_validation_rejected"
            )
            self.assertNotIn("output", report["methods"][name])

    def test_explicit_sanitized_wav_uses_real_audio_parser_and_intervals(self):
        output = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / "synthetic.wav"
            filename.write_bytes(wav_bytes())
            with patch(
                "analysis.management.commands.evaluate_local_analysis.LocalClient"
            ) as client:
                client.return_value.infer.return_value = {"items": []}
                client.return_value.usage = {"request_count": 1}
                client.return_value.request.return_value = {
                    "text": "Synthetic speech candidate",
                    "segments": [
                        {"start": 0, "end": 1, "text": "Synthetic speech candidate"}
                    ],
                }
                call_command(
                    "evaluate_local_analysis", audio=str(filename), stdout=output
                )
        report = json.loads(output.getvalue())
        result = report["methods"]["transcription"]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["output"]["items"][0]["anchors"][0]["end_seconds"], 1)
        self.assertEqual(client.return_value.request.call_args.args[0], "transcription")

    def test_missing_audio_reports_safe_error_without_printing_path(self):
        output = StringIO()
        with patch(
            "analysis.management.commands.evaluate_local_analysis.LocalClient"
        ) as client:
            client.return_value.infer.return_value = {"items": []}
            client.return_value.usage = {}
            with self.assertRaisesMessage(CommandError, "qualification failures"):
                call_command(
                    "evaluate_local_analysis",
                    audio="/missing/private-name.wav",
                    stdout=output,
                )
        report = json.loads(output.getvalue())
        self.assertEqual(report["summary"]["failed"], ["transcription"])
        self.assertEqual(
            report["methods"]["transcription"]["error_code"],
            "qualification_audio_unavailable",
        )
        self.assertNotIn("private-name", output.getvalue())
