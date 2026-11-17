"""Automatic AI selection shares the transport's configuration requirements."""

from django.test import SimpleTestCase, override_settings

from core.analysis.gateway import gateway_configured
from core.analysis.tests.test_gateway import CONFIG


class GatewayConfigurationTests(SimpleTestCase):
    @override_settings(PANDECT_AI_GATEWAY=CONFIG)
    def test_complete_workers_configuration_is_available(self):
        self.assertTrue(gateway_configured("text", "content_events"))
        self.assertFalse(gateway_configured("transcription", "transcription"))
        self.assertFalse(gateway_configured("unknown", "content_events"))

    def test_missing_or_partial_workers_configuration_is_unavailable(self):
        configs = [
            {},
            CONFIG | {"account": ""},
            CONFIG | {"gateway": "../wrong"},
            CONFIG | {"gateway_token": ""},
            CONFIG | {"workers_token": ""},
            CONFIG | {"models": {"text": "@cf/../wrong"}},
        ]
        for config in configs:
            with (
                self.subTest(config=config.keys()),
                override_settings(PANDECT_AI_GATEWAY=config),
            ):
                self.assertFalse(gateway_configured("text", "content_events"))

    def test_openai_uses_gateway_auth_and_key_alias_without_workers_token(self):
        config = CONFIG | {
            "text_provider": "openai",
            "models": {"text": "gpt-synthetic"},
            "workers_token": "",
            "openai_key_alias": "synthetic-key",
        }
        with override_settings(PANDECT_AI_GATEWAY=config):
            self.assertTrue(gateway_configured("text", "content_events"))
        with override_settings(
            PANDECT_AI_GATEWAY=config | {"openai_key_alias": "bad/key"}
        ):
            self.assertFalse(gateway_configured("text", "content_events"))
