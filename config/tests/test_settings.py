import os
import runpy
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase


class StartupTests(SimpleTestCase):
    def test_startup_rejects_missing_secret_even_in_debug_mode(self):
        with (
            patch.dict(os.environ, {"DJANGO_DEBUG": "true"}, clear=True),
            self.assertRaisesMessage(ImproperlyConfigured, "DJANGO_SECRET_KEY"),
        ):
            runpy.run_path(Path(__file__).parents[1] / "settings.py")
