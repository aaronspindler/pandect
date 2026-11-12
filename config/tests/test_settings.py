import os
import runpy
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import RequestFactory, SimpleTestCase

from accounts.adapter import PandectAccountAdapter


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

    def test_local_first_defaults_do_not_configure_public_services(self):
        settings = self.load_settings()
        self.assertEqual(settings["PANDECT_MAP_TILE_URL"], "")
        self.assertEqual(settings["PANDECT_MAP_TILE_ORIGIN"], "")
        self.assertFalse(settings["PANDECT_GEOCODER_LOCAL"])
        self.assertFalse(settings["PANDECT_MAP_CONTEXT_LOCAL"])
        self.assertEqual(settings["PANDECT_LOCAL_AI"]["text"]["url"], "")
        self.assertEqual(settings["PANDECT_LOCAL_AI"]["transcription"]["url"], "")
        self.assertEqual(settings["PANDECT_LOCAL_AI"]["token"], "")

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

    def test_local_inference_limits_are_separate_from_gateway_limits(self):
        settings = self.load_settings(
            PANDECT_AI_TIMEOUT="60",
            PANDECT_LOCAL_AI_TIMEOUT="9999",
            PANDECT_LOCAL_TEXT_CONTEXT_TOKENS="8192",
            PANDECT_LOCAL_AI_MAX_OUTPUT_TOKENS="1024",
        )
        self.assertEqual(settings["PANDECT_AI_GATEWAY"]["timeout"], 60)
        self.assertEqual(settings["PANDECT_LOCAL_AI"]["timeout"], 1800)
        self.assertEqual(settings["PANDECT_LOCAL_AI"]["text"]["context_tokens"], 8192)
        self.assertEqual(settings["PANDECT_LOCAL_AI"]["max_output_tokens"], 1024)
