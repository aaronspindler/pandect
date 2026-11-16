from unittest.mock import patch

from django.conf import settings
from django.db import OperationalError, connection
from django.test import TestCase, override_settings
from django.urls import reverse


class HealthTests(TestCase):
    def test_readiness_checks_postgresql_without_authentication(self):
        self.assertEqual(connection.vendor, "postgresql")
        response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "status": "ok",
                "version": settings.APP_VERSION,
                "commit": settings.APP_COMMIT_SHA,
            },
        )
        self.assertIn("no-store", response.headers["Cache-Control"])

    @override_settings(
        APP_VERSION="v1.23",
        APP_COMMIT_SHA="0123456789abcdef0123456789abcdef01234567",
    )
    def test_readiness_identifies_the_running_release(self):
        response = self.client.get(reverse("health"))
        self.assertEqual(response.json()["version"], "v1.23")
        self.assertEqual(
            response.json()["commit"], "0123456789abcdef0123456789abcdef01234567"
        )

    def test_database_failure_returns_503_without_connection_details(self):
        with patch(
            "config.views.connection.cursor",
            side_effect=OperationalError("sensitive connection details"),
        ):
            response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable"})

    def test_readiness_does_not_accept_writes(self):
        self.assertEqual(self.client.post(reverse("health")).status_code, 405)
