import logging
from unittest.mock import patch

import pytest
from django.db import DatabaseError
from django.test import Client
from django.urls import reverse

pytestmark = pytest.mark.django_db


def test_home_renders_complete_semantic_page(client):
    response = client.get(reverse("home"))

    assert response.status_code == 200
    assert response.templates[0].name == "core/home.html"
    content = response.content.decode()
    assert content.startswith("<!doctype html>")
    assert "<header" in content
    assert "<nav" in content
    assert '<main id="main-content"' in content
    assert "<footer" in content
    assert 'href="#main-content">Skip to main content</a>' in content
    assert 'href="/" aria-current="page">Home</a>' in content
    assert "/static/vendor/htmx/htmx-2.0.10.min.js" in content
    assert "X-CSRFToken" in content
    assert "NOTPROVIDED" not in content
    assert "http://" not in content
    assert "https://" not in content


def test_status_post_has_full_page_fallback_with_csrf():
    client = Client(enforce_csrf_checks=True)
    home = client.get(reverse("home"))
    token = home.cookies["csrftoken"].value

    response = client.post(
        reverse("runtime-status"),
        {"csrfmiddlewaretoken": token},
    )

    assert response.status_code == 200
    assert response.templates[0].name == "core/status.html"
    assert response.content.decode().startswith("<!doctype html>")
    assert "Runtime status" in response.content.decode()


def test_status_htmx_post_returns_only_the_partial_and_accepts_csrf_header():
    client = Client(enforce_csrf_checks=True)
    home = client.get(reverse("home"))
    token = home.cookies["csrftoken"].value

    response = client.post(
        reverse("runtime-status"),
        HTTP_HX_REQUEST="true",
        HTTP_X_CSRFTOKEN=token,
    )

    assert response.status_code == 200
    assert response.templates[0].name == "core/partials/runtime_status.html"
    content = response.content.decode()
    assert content.lstrip().startswith('<section id="runtime-status"')
    assert "<!doctype html>" not in content


def test_status_post_rejects_a_missing_csrf_token():
    client = Client(enforce_csrf_checks=True)

    response = client.post(reverse("runtime-status"), HTTP_HX_REQUEST="true")

    assert response.status_code == 403


def test_status_endpoint_rejects_get(client):
    assert client.get(reverse("runtime-status")).status_code == 405


def test_health_is_dependency_free_plain_text(client):
    response = client.get(reverse("health"))

    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/plain")
    assert response.content == b"ok\n"


def test_readiness_checks_the_database(client):
    response = client.get(reverse("readiness"))

    assert response.status_code == 200
    assert response.content == b"ready\n"


def test_readiness_fails_closed_without_exposing_database_details(client, caplog):
    with (
        patch(
            "pandect.core.views.connection.cursor",
            side_effect=DatabaseError("sensitive machine path"),
        ),
        caplog.at_level(logging.WARNING),
    ):
        response = client.get(reverse("readiness"))

    assert response.status_code == 503
    assert response.content == b"not ready\n"
    assert "Application database readiness check failed" in caplog.text
    assert "sensitive machine path" not in caplog.text
