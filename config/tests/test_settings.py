import os
import runpy
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import RequestFactory, SimpleTestCase

from accounts.adapter import PandectAccountAdapter
from core.analysis.execution import gateway_execution


class StartupTests(SimpleTestCase):
    def test_startup_rejects_missing_secret_even_in_debug_mode(self):
        with (
            patch.dict(os.environ, {"DJANGO_DEBUG": "true"}, clear=True),
            self.assertRaisesMessage(ImproperlyConfigured, "DJANGO_SECRET_KEY"),
        ):
            runpy.run_path(Path(__file__).parents[1] / "settings.py")

    def load_settings(self, **values):
        with patch.dict(
            os.environ, {"DJANGO_SECRET_KEY": "synthetic", **values}, clear=True
        ):
            return runpy.run_path(Path(__file__).parents[1] / "settings.py")

    def test_rate_limit_client_ip_requires_a_configured_trusted_proxy_chain(self):
        request = RequestFactory().get(
            "/accounts/login/",
            REMOTE_ADDR="203.0.113.3",
            HTTP_X_FORWARDED_FOR="192.0.2.1, 198.51.100.2, 203.0.113.2",
        )
        adapter = PandectAccountAdapter()
        for values, expected in (
            ({}, "203.0.113.3"),
            ({"PANDECT_TRUSTED_PROXY_COUNT": "2"}, "198.51.100.2"),
        ):
            settings = self.load_settings(**values)
            with self.settings(
                ALLAUTH_TRUSTED_PROXY_COUNT=settings["ALLAUTH_TRUSTED_PROXY_COUNT"]
            ):
                self.assertEqual(adapter.get_client_ip(request), expected)

    def test_proxy_count_must_be_a_nonnegative_integer(self):
        for invalid in ("-1", "two"):
            with (
                self.subTest(invalid=invalid),
                self.assertRaisesMessage(
                    ImproperlyConfigured, "PANDECT_TRUSTED_PROXY_COUNT"
                ),
            ):
                self.load_settings(PANDECT_TRUSTED_PROXY_COUNT=invalid)

    def test_defaults_leave_cloud_destinations_unconfigured(self):
        settings = self.load_settings()
        self.assertEqual(settings["PANDECT_MAP_TILE_URL"], "")
        self.assertEqual(settings["PANDECT_MAP_TILE_ORIGIN"], "")
        self.assertFalse(settings["PANDECT_GEOCODER_LOCAL"])
        self.assertFalse(settings["PANDECT_MAP_CONTEXT_LOCAL"])
        self.assertFalse(settings["PANDECT_AI_GATEWAY"]["account"])
        self.assertNotIn("PANDECT_LOCAL_AI", settings)
        self.assertNotIn("PANDECT_DIRECT_AI", settings)

    def test_default_gateway_model_depends_on_provider_and_operation(self):
        workers = self.load_settings()["PANDECT_AI_GATEWAY"]
        with self.settings(PANDECT_AI_GATEWAY=workers):
            route = gateway_execution("text", "content_events")
            self.assertEqual(route["model"], "@cf/meta/llama-3.3-70b-instruct-fp8-fast")
        openai = self.load_settings(PANDECT_AI_TEXT_PROVIDER="openai")[
            "PANDECT_AI_GATEWAY"
        ]
        with self.settings(PANDECT_AI_GATEWAY=openai):
            self.assertEqual(
                gateway_execution("text", "content_events")["model"], "gpt-6-luna"
            )
            self.assertFalse(gateway_execution("text", "unknown_operation")["model"])
            self.assertEqual(
                gateway_execution("transcription", "transcription")["model"],
                "@cf/openai/whisper-large-v3-turbo",
            )

    def test_service_locality_requires_explicit_operator_configuration(self):
        settings = self.load_settings(
            PANDECT_GEOCODER_URL="http://private-geocoder:8080",
            PANDECT_MAP_CONTEXT_URL="http://private-overpass:8080",
        )
        self.assertFalse(settings["PANDECT_GEOCODER_LOCAL"])
        self.assertFalse(settings["PANDECT_MAP_CONTEXT_LOCAL"])
        settings = self.load_settings(
            PANDECT_GEOCODER_LOCAL="true",
            PANDECT_MAP_CONTEXT_LOCAL="true",
            PANDECT_MAP_TILE_URL="https://tiles.example.test/{z}/{x}/{y}.png",
        )
        self.assertTrue(settings["PANDECT_GEOCODER_LOCAL"])
        self.assertTrue(settings["PANDECT_MAP_CONTEXT_LOCAL"])
        self.assertEqual(
            settings["PANDECT_MAP_TILE_ORIGIN"], "https://tiles.example.test"
        )

    def test_nonempty_tile_templates_still_require_safe_https_urls(self):
        for url in (
            "http://tiles.example.test/{z}/{x}/{y}.png",
            "https://user:secret@tiles.example.test/{z}/{x}/{y}.png",
            "https://tiles.example.test/{z}/{x}/{y}.png?token=secret",
            "https://tiles.example.test/{z}/{x}.png",
        ):
            with self.subTest(url=url), self.assertRaises(ImproperlyConfigured):
                self.load_settings(PANDECT_MAP_TILE_URL=url)

    def test_product_limits_are_fixed_even_with_retired_environment_overrides(self):
        retired = (
            "PANDECT_MAX_FILE_BYTES",
            "PANDECT_MAX_TEXT_BYTES",
            "PANDECT_MAX_BATCH_FILES",
            "PANDECT_INVITATION_DAYS",
            "PANDECT_AI_TIMEOUT",
            "PANDECT_AI_DAILY_CASE_REQUESTS",
            "PANDECT_AI_MAX_REQUEST_BYTES",
            "PANDECT_AI_MAX_RESPONSE_BYTES",
            "PANDECT_LOCAL_AI_TIMEOUT",
            "PANDECT_LOCAL_AI_MAX_REQUEST_BYTES",
            "PANDECT_LOCAL_AI_MAX_RESPONSE_BYTES",
            "PANDECT_LOCAL_TEXT_CONTEXT_TOKENS",
            "PANDECT_LOCAL_AI_MAX_OUTPUT_TOKENS",
            "PANDECT_GEOCODER_TIMEOUT",
            "PANDECT_MAP_CONTEXT_TIMEOUT",
        )
        settings = self.load_settings(**dict.fromkeys(retired, "not-an-integer"))
        self.assertEqual(settings["PANDECT_MAX_FILE_BYTES"], 1024**3)
        self.assertEqual(settings["PANDECT_MAX_TEXT_BYTES"], 10 * 1024**2)
        self.assertEqual(settings["PANDECT_MAX_BATCH_FILES"], 100)
        self.assertEqual(settings["PANDECT_INVITATION_DAYS"], 7)
        self.assertEqual(settings["PANDECT_GEOCODER_TIMEOUT"], 10)
        self.assertEqual(settings["PANDECT_MAP_CONTEXT_TIMEOUT"], 10)
        gateway = settings["PANDECT_AI_GATEWAY"]
        self.assertEqual(gateway["timeout"], 60)
        self.assertNotIn("daily_requests", gateway)
        self.assertEqual(gateway["max_request_bytes"], 8 * 1024**2)
        self.assertEqual(gateway["max_response_bytes"], 2 * 1024**2)

    def test_stale_direct_keys_never_select_a_direct_route(self):
        config = self.load_settings(
            OPENAI_API_KEY="ignored", ANTHROPIC_API_KEY="ignored"
        )
        self.assertEqual(config["PANDECT_AI_GATEWAY"]["text_provider"], "workers-ai")
