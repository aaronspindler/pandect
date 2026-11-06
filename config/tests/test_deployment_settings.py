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

    def test_hsts_and_cross_origin_csrf_trust_require_configuration(self):
        defaults = self.load_settings()
        self.assertEqual(defaults["SECURE_HSTS_SECONDS"], 0)
        self.assertEqual(defaults["CSRF_TRUSTED_ORIGINS"], [])
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
