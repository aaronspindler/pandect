"""ASGI entry point for the dedicated case event listener."""

import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from cases.events import application  # noqa: E402, F401
