import base64
import hashlib
from io import StringIO
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.contrib.staticfiles import finders
from django.core.management import call_command


def test_django_system_checks_pass():
    output = StringIO()

    call_command("check", stdout=output)

    assert "System check identified no issues" in output.getvalue()


def test_test_settings_force_hermetic_local_state():
    assert settings.DATA_DIR.name.startswith("pandect-tests-")
    assert settings.SECRET_KEY == "pandect-hermetic-test-secret"


def test_application_and_huey_use_separate_sqlite_files():
    application_path = settings.APPLICATION_DATABASE_PATH
    huey_path = Path(settings.HUEY["filename"])

    assert application_path.name == "application.sqlite3"
    assert huey_path.name == "huey.sqlite3"
    assert application_path != huey_path
    assert application_path.parent == huey_path.parent == settings.DATA_DIR
    assert settings.DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3"
    assert settings.DATABASES["default"]["OPTIONS"]["timeout"] == 5


def test_foundation_does_not_install_auth_admin_or_sessions():
    assert "django.contrib.auth" not in settings.INSTALLED_APPS
    assert "django.contrib.admin" not in settings.INSTALLED_APPS
    assert "django.contrib.sessions" not in settings.INSTALLED_APPS
    assert list(apps.get_app_config("core").get_models()) == []


def test_huey_is_sqlite_backed_and_eager_in_tests():
    assert settings.HUEY["huey_class"] == "huey.SqliteHuey"
    assert settings.HUEY["immediate"] is True
    assert settings.HUEY["consumer"]["workers"] == 1
    assert settings.HUEY["consumer"]["periodic"] is True


def test_static_assets_are_local_and_discoverable():
    htmx_path = finders.find("vendor/htmx/htmx-2.0.10.min.js")
    css_path = finders.find("css/pandect.css")

    assert htmx_path is not None
    assert css_path is not None
    htmx_bytes = Path(htmx_path).read_bytes()
    assert "htmx" in htmx_bytes[:100].decode()
    # apply_patch preserves a conventional final newline; upstream does not.
    digest = base64.b64encode(hashlib.sha384(htmx_bytes.removesuffix(b"\n")).digest())
    assert digest.decode() == (
        "H5SrcfygHmAuTDZphMHqBJLc3FhssKjG7w/CeCpFReSfwBWDTKpkzPP8c+cLsK+V"
    )


def test_whitenoise_serves_collected_static_files():
    assert "whitenoise.middleware.WhiteNoiseMiddleware" in settings.MIDDLEWARE
    assert settings.STATIC_ROOT.name == "staticfiles"


def test_clickjacking_protection_is_enabled():
    assert (
        "django.middleware.clickjacking.XFrameOptionsMiddleware" in settings.MIDDLEWARE
    )
    assert settings.X_FRAME_OPTIONS == "DENY"
