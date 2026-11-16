import os
import runpy
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.http import HttpResponse
from django.middleware.security import SecurityMiddleware
from django.test import RequestFactory, SimpleTestCase, override_settings


class DeploymentSettingsTests(SimpleTestCase):
    def load_settings(self, **values):
        with patch.dict(
            os.environ, {"DJANGO_SECRET_KEY": "synthetic", **values}, clear=True
        ):
            return runpy.run_path(Path(__file__).parents[1] / "settings.py")

    def test_forwarded_https_is_trusted_only_when_explicitly_enabled(self):
        request = RequestFactory().get("/", HTTP_X_FORWARDED_PROTO="https")
        defaults = self.load_settings()
        with override_settings(
            SECURE_PROXY_SSL_HEADER=defaults["SECURE_PROXY_SSL_HEADER"]
        ):
            self.assertFalse(request.is_secure())
        production = self.load_settings(DJANGO_TRUST_PROXY_HEADERS="true")
        with override_settings(
            SECURE_PROXY_SSL_HEADER=production["SECURE_PROXY_SSL_HEADER"]
        ):
            self.assertTrue(request.is_secure())

    def test_https_redirect_preserves_loopback_readiness(self):
        production = self.load_settings(DJANGO_SECURE_SSL_REDIRECT="true")
        with override_settings(
            ALLOWED_HOSTS=["openpandect.com", "127.0.0.1"],
            SECURE_SSL_REDIRECT=production["SECURE_SSL_REDIRECT"],
            SECURE_REDIRECT_EXEMPT=production["SECURE_REDIRECT_EXEMPT"],
        ):
            middleware = SecurityMiddleware(lambda request: HttpResponse("ok"))
            factory = RequestFactory()
            response = middleware(factory.get("/", HTTP_HOST="openpandect.com"))
            self.assertEqual(response.status_code, 301)
            self.assertEqual(response.url, "https://openpandect.com/")
            health = middleware(factory.get("/health/", HTTP_HOST="127.0.0.1"))
            self.assertEqual(health.status_code, 200)

    def test_site_origin_supplies_hosts_csrf_and_sender_defaults(self):
        settings = self.load_settings(
            PANDECT_PUBLIC_ORIGIN="https://cases.example.test:8443/"
        )
        self.assertEqual(
            settings["ALLOWED_HOSTS"],
            ["cases.example.test", "localhost", "127.0.0.1", "[::1]"],
        )
        self.assertEqual(
            settings["CSRF_TRUSTED_ORIGINS"], ["https://cases.example.test:8443"]
        )
        self.assertEqual(settings["DEFAULT_FROM_EMAIL"], "pandect@cases.example.test")
        self.assertFalse(settings["DEBUG"])
        self.assertIsNone(settings["SECURE_PROXY_SSL_HEADER"])
        self.assertFalse(settings["SECURE_SSL_REDIRECT"])

    def test_empty_overrides_use_defaults_and_explicit_overrides_replace_them(self):
        settings = self.load_settings(
            PANDECT_PUBLIC_ORIGIN="https://cases.example.test",
            DJANGO_ALLOWED_HOSTS="",
            DJANGO_CSRF_TRUSTED_ORIGINS="",
            DJANGO_DEFAULT_FROM_EMAIL="",
        )
        self.assertIn("cases.example.test", settings["ALLOWED_HOSTS"])
        self.assertEqual(
            settings["CSRF_TRUSTED_ORIGINS"], ["https://cases.example.test"]
        )
        self.assertEqual(settings["DEFAULT_FROM_EMAIL"], "pandect@cases.example.test")
        explicit = self.load_settings(
            PANDECT_PUBLIC_ORIGIN="https://cases.example.test",
            DJANGO_ALLOWED_HOSTS="other.example.test, 127.0.0.1",
            DJANGO_CSRF_TRUSTED_ORIGINS="https://other.example.test",
            DJANGO_DEFAULT_FROM_EMAIL="operator@example.test",
        )
        self.assertEqual(explicit["ALLOWED_HOSTS"], ["other.example.test", "127.0.0.1"])
        self.assertEqual(
            explicit["CSRF_TRUSTED_ORIGINS"], ["https://other.example.test"]
        )
        self.assertEqual(explicit["DEFAULT_FROM_EMAIL"], "operator@example.test")

    def test_local_and_ip_origins_keep_safe_sender_and_valid_health_hosts(self):
        for origin, host in (
            ("http://localhost:8000", "localhost"),
            ("http://127.0.0.1:8001", "127.0.0.1"),
            ("http://[::1]:8002", "[::1]"),
        ):
            with self.subTest(origin=origin):
                settings = self.load_settings(PANDECT_PUBLIC_ORIGIN=origin)
                self.assertEqual(settings["DEFAULT_FROM_EMAIL"], "pandect@localhost")
                with override_settings(ALLOWED_HOSTS=settings["ALLOWED_HOSTS"]):
                    self.assertEqual(
                        RequestFactory().get("/", HTTP_HOST=host).get_host(), host
                    )
                    self.assertEqual(
                        RequestFactory()
                        .get("/health/", HTTP_HOST="127.0.0.1")
                        .get_host(),
                        "127.0.0.1",
                    )

    def test_invalid_public_origins_fail_before_deriving_defaults(self):
        for origin in (
            "ftp://example.test",
            "https://example.test/path",
            "https://example.test?query=1",
            "https://example.test#fragment",
            "https://user:password@example.test",
            "https://example.test:bad",
            "https://example.test:65536",
            "https://[::1",
            "https://example.test\n",
            "https://*.example.test",
            "https://",
        ):
            with (
                self.subTest(origin=origin),
                self.assertRaisesMessage(ImproperlyConfigured, "PANDECT_PUBLIC_ORIGIN"),
            ):
                self.load_settings(PANDECT_PUBLIC_ORIGIN=origin)

    def test_hsts_defaults_to_one_day_for_https_origins_and_allows_overrides(self):
        defaults = self.load_settings()
        self.assertEqual(defaults["SECURE_HSTS_SECONDS"], 0)
        self.assertEqual(defaults["CSRF_TRUSTED_ORIGINS"], ["http://localhost:8000"])
        https = self.load_settings(PANDECT_PUBLIC_ORIGIN="https://cases.example.test")
        self.assertEqual(https["SECURE_HSTS_SECONDS"], 86400)
        for override, expected in (("", 86400), ("0", 0), ("3600", 3600)):
            with self.subTest(override=override):
                configured = self.load_settings(
                    PANDECT_PUBLIC_ORIGIN="https://cases.example.test",
                    DJANGO_SECURE_HSTS_SECONDS=override,
                )
                self.assertEqual(configured["SECURE_HSTS_SECONDS"], expected)
        production = self.load_settings(
            DJANGO_SECURE_HSTS_SECONDS="86400",
            DJANGO_CSRF_TRUSTED_ORIGINS="https://openpandect.com, , https://www.example.test",
        )
        self.assertEqual(production["SECURE_HSTS_SECONDS"], 86400)
        self.assertEqual(
            production["CSRF_TRUSTED_ORIGINS"],
            ["https://openpandect.com", "https://www.example.test"],
        )
        with self.assertRaisesMessage(ImproperlyConfigured, "must not be negative"):
            self.load_settings(DJANGO_SECURE_HSTS_SECONDS="-1")

    def test_https_response_emits_hsts_behind_an_explicitly_trusted_proxy(self):
        production = self.load_settings(
            PANDECT_PUBLIC_ORIGIN="https://cases.example.test",
            DJANGO_TRUST_PROXY_HEADERS="true",
        )
        with override_settings(
            SECURE_HSTS_SECONDS=production["SECURE_HSTS_SECONDS"],
            SECURE_PROXY_SSL_HEADER=production["SECURE_PROXY_SSL_HEADER"],
        ):
            middleware = SecurityMiddleware(lambda request: HttpResponse("ok"))
            request = RequestFactory().get(
                "/", HTTP_HOST="cases.example.test", HTTP_X_FORWARDED_PROTO="https"
            )
            response = middleware(request)
            self.assertEqual(response["Strict-Transport-Security"], "max-age=86400")
            local = middleware(RequestFactory().get("/"))
            self.assertNotIn("Strict-Transport-Security", local)
