"""Exercise real app discovery and release assets after removing root directories."""

import tempfile
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.contrib import admin
from django.contrib.staticfiles import finders
from django.contrib.staticfiles.storage import staticfiles_storage
from django.core.management import call_command
from django.template.loader import get_template
from django.test import SimpleTestCase, override_settings

from config.settings import STORAGES
from core.evidence.types import KINDS


class AppResourceDiscoveryTests(SimpleTestCase):
    def test_templates_resolve_from_their_owning_apps(self):
        for app_label in (
            "accounts",
            "cases",
            "core",
            "text",
            "image",
            "audio",
            "document",
            "geo",
            "phone",
            "people",
            "vehicle",
            "socialmedia",
            "marketing",
            "ui",
        ):
            root = Path(apps.get_app_config(app_label).path) / "templates"
            for path in root.rglob("*.html"):
                name = path.relative_to(root).as_posix()
                with self.subTest(template=name, app=app_label):
                    self.assertEqual(Path(get_template(name).origin.name), path)

    @override_settings(DEBUG=False, STATICFILES_DIRS=[], STORAGES=STORAGES)
    def test_app_assets_collect_with_resolvable_release_urls(self):
        assets = (
            "ui/primitives.css",
            "ui/app.css",
            "ui/app.js",
            "ui/live.js",
            "ui/brand/logo-oxblood.png",
            "ui/icons/files.svg",
            "core/evidence.js",
            "core/admin.css",
            "image/preview.js",
            "marketing/marketing.css",
            "geo/map.css",
            "geo/map.js",
            "geo/vendor/leaflet/leaflet.css",
            "geo/vendor/leaflet/leaflet.js",
            "geo/vendor/leaflet/images/marker-icon.png",
        )
        with tempfile.TemporaryDirectory(prefix="pandect-app-assets-") as directory:
            with override_settings(STATIC_ROOT=directory):
                call_command("collectstatic", interactive=False, verbosity=0)
                for name in assets:
                    with self.subTest(asset=name):
                        self.assertEqual(len(finders.find(name, find_all=True)), 1)
                        self.assertNotEqual(
                            staticfiles_storage.url(name), settings.STATIC_URL + name
                        )
                        self.assertTrue(
                            (
                                Path(directory) / staticfiles_storage.stored_name(name)
                            ).is_file()
                        )
                for model in KINDS.values():
                    with self.subTest(admin=model._meta.label):
                        media = str(admin.site.get_model_admin(model).media)
                        self.assertIn(staticfiles_storage.url("core/admin.css"), media)
