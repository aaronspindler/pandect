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

    def test_direct_keys_alone_do_not_authorize_a_provider(self):
        loaded = self.load_settings(
            OPENAI_API_KEY="synthetic", ANTHROPIC_API_KEY="synthetic"
        )
        with self.settings(PANDECT_AI_GATEWAY=loaded["PANDECT_AI_GATEWAY"]):
            self.assertFalse(gateway_configured("text", "content_events"))
            self.assertFalse(gateway_configured("transcription", "transcription"))

    def test_workers_and_upstream_routes_require_the_configured_gateway(self):
        for selection in ("openai", "workers-ai"):
            with self.subTest(selection=selection):
                loaded = self.load_settings(
                    PANDECT_AI_ACCOUNT_ID="a" * 32,
                    PANDECT_AI_GATEWAY_ID="synthetic",
                    PANDECT_AI_GATEWAY_TOKEN="synthetic-gateway-token",
                    PANDECT_AI_WORKERS_TOKEN="synthetic-workers-token",
                    PANDECT_AI_STORED_KEY_PROVIDERS="openai",
                    PANDECT_AI_TEXT_PROVIDER=selection,
                )
                with self.settings(PANDECT_AI_GATEWAY=loaded["PANDECT_AI_GATEWAY"]):
                    self.assertTrue(gateway_configured("text", "content_events"))
                    self.assertEqual(
                        gateway_execution("text", "content_events")["gateway"],
                        "synthetic",
                    )
