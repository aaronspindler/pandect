from unittest.mock import patch

from django.db import OperationalError, connection
from django.test import TestCase
from django.urls import reverse


class HealthTests(TestCase):
    def test_readiness_checks_postgresql_without_authentication(self):
        self.assertEqual(connection.vendor, "postgresql")
        response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertIn("no-store", response.headers["Cache-Control"])

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
