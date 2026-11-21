import os
import runpy
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from core.analysis.execution import gateway_execution
from core.analysis.gateway import gateway_configured


class ExternalAISettingsTests(SimpleTestCase):
    def load_settings(self, **values):
        with patch.dict(
            os.environ, {"DJANGO_SECRET_KEY": "synthetic", **values}, clear=True
        ):
            return runpy.run_path(Path(__file__).parents[1] / "settings.py")

    def test_single_api_key_configures_direct_text_analysis_without_a_gateway(self):
        for variable, provider, model in (
            ("OPENAI_API_KEY", "openai", "gpt-6-luna"),
            ("ANTHROPIC_API_KEY", "anthropic", "claude-haiku-5-5"),
        ):
            with self.subTest(provider=provider):
                loaded = self.load_settings(**{variable: "synthetic-key"})
                with self.settings(
                    PANDECT_AI_GATEWAY=loaded["PANDECT_AI_GATEWAY"],
                    PANDECT_DIRECT_AI=loaded["PANDECT_DIRECT_AI"],
                ):
                    route = gateway_execution("text", "content_events")
                    self.assertTrue(gateway_configured("text", "content_events"))
                    self.assertEqual(
                        (route["provider"], route["model"]), (provider, model)
                    )
                    self.assertFalse(
                        gateway_configured("transcription", "transcription")
                    )

    def test_two_keys_require_selection_even_with_a_configured_gateway(self):
        for selection, expected_provider in (
            ("", None),
            ("anthropic-direct", "anthropic"),
            ("openai", "openai"),
            ("workers-ai", "workers-ai"),
        ):
            with self.subTest(selection=selection):
                loaded = self.load_settings(
                    OPENAI_API_KEY="synthetic-openai-key",
                    ANTHROPIC_API_KEY="synthetic-anthropic-key",
                    PANDECT_AI_ACCOUNT_ID="a" * 32,
                    PANDECT_AI_GATEWAY_ID="synthetic",
                    PANDECT_AI_GATEWAY_TOKEN="synthetic-gateway-token",
                    PANDECT_AI_WORKERS_TOKEN="synthetic-workers-token",
                    PANDECT_AI_TEXT_PROVIDER=selection,
                )
                with self.settings(
                    PANDECT_AI_GATEWAY=loaded["PANDECT_AI_GATEWAY"],
                    PANDECT_DIRECT_AI=loaded["PANDECT_DIRECT_AI"],
                ):
                    configured = gateway_configured("text", "content_events")
                    self.assertEqual(configured, expected_provider is not None)
                    self.assertTrue(
                        gateway_configured("transcription", "transcription")
                    )
                    if configured:
                        route = gateway_execution("text", "content_events")
                        self.assertEqual(route["provider"], expected_provider)
                        if selection in {"openai", "workers-ai"}:
                            self.assertEqual(route["gateway"], "synthetic")
