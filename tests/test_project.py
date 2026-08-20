import base64
import hashlib
import json
import os
import socket
import subprocess
import sys
from io import StringIO
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.contrib.staticfiles import finders
from django.core.management import call_command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NATIVE_THREAD_ENVIRONMENT = (
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "OMP_NUM_THREADS",
    "OMP_THREAD_LIMIT",
    "OPENBLAS_NUM_THREADS",
)


def test_django_system_checks_pass():
    output = StringIO()

    call_command("check", stdout=output)

    assert "System check identified no issues" in output.getvalue()


def test_test_settings_force_hermetic_local_state():
    assert settings.DATA_DIR.name.startswith("pandect-tests-")
    assert settings.SECRET_KEY == "pandect-hermetic-test-secret"
    assert settings.CLOUDFLARE_AI_ENABLED is False
    assert settings.CLOUDFLARE_ACCOUNT_ID == ""
    assert settings.CLOUDFLARE_API_TOKEN == ""


def test_test_settings_ignore_ambient_cloudflare_configuration():
    environment = os.environ.copy()
    environment.update(
        {
            "DJANGO_SETTINGS_MODULE": "pandect.config.test_settings",
            "PANDECT_CLOUDFLARE_AI_ENABLED": "true",
            "PANDECT_CLOUDFLARE_ACCOUNT_ID": "ambient-account",
            "PANDECT_CLOUDFLARE_API_TOKEN": "ambient-token",
        }
    )
    completed = subprocess.run(
        (
            sys.executable,
            "-c",
            (
                "import json; from django.conf import settings; "
                "print(json.dumps([settings.CLOUDFLARE_AI_ENABLED, "
                "settings.CLOUDFLARE_ACCOUNT_ID, settings.CLOUDFLARE_API_TOKEN]))"
            ),
        ),
        check=True,
        capture_output=True,
        env=environment,
        text=True,
        timeout=15,
    )

    assert json.loads(completed.stdout) == [False, "", ""]


def test_test_suite_blocks_real_outbound_network_access():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        try:
            connection.connect(("127.0.0.1", 9))
        except AssertionError as error:
            assert "outbound network access is disabled" in str(error)
        else:  # pragma: no cover - the autouse guard must always intercept this
            raise AssertionError("the hermetic network guard did not run")


def test_application_and_huey_use_separate_sqlite_files():
    application_path = settings.APPLICATION_DATABASE_PATH
    huey_path = Path(settings.HUEY["filename"])

    assert application_path.name == "application.sqlite3"
    assert huey_path.name == "huey.sqlite3"
    assert application_path != huey_path
    assert application_path.parent == huey_path.parent == settings.DATA_DIR
    assert settings.DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3"
    assert settings.DATABASES["default"]["OPTIONS"]["timeout"] == 5


def test_catalog_media_uses_the_persistent_local_data_directory():
    assert settings.MEDIA_ROOT == settings.DATA_DIR / "media"
    assert Path(settings.FILE_UPLOAD_TEMP_DIR) == settings.WORK_ROOT / "uploads"
    assert Path(settings.FILE_UPLOAD_TEMP_DIR).is_dir()


def test_catalogue_models_are_owned_by_focused_apps():
    from pandect.audio.models import Audio
    from pandect.catalog.models import CatalogItem
    from pandect.events.models import Event
    from pandect.messages.models import Message
    from pandect.notes.models import Note
    from pandect.photos.models import Image
    from pandect.videos.models import Video

    assert {
        model._meta.app_label
        for model in (CatalogItem, Event, Message, Note, Image, Video, Audio)
    } == {"audio", "catalog", "events", "messages", "notes", "photos", "videos"}


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


def test_runtime_and_tools_containers_cap_native_thread_pools():
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text()
    runtime = dockerfile.split(" AS runtime", 1)[1].split(" AS tools", 1)[0]
    tools = dockerfile.split(" AS tools", 1)[1]
    compose = (PROJECT_ROOT / "compose.yaml").read_text()

    for variable in NATIVE_THREAD_ENVIRONMENT:
        assert f"{variable}=1" in runtime
        assert f"{variable}=1" in tools
        assert compose.count(f'{variable}: "1"') == 2


def test_tools_container_has_no_runtime_network_namespace():
    compose = (PROJECT_ROOT / "compose.yaml").read_text()
    tools = compose.split("  tools:\n", 1)[1]

    assert "    network_mode: none\n" in tools
    assert "network_mode: none" not in compose.split("  tools:\n", 1)[0]


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
