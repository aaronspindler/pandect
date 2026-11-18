"""Indexed case search across native socialmedia records."""

from django.db import migrations

from core.migrations._case_search_indexes import search_indexes


class Migration(migrations.Migration):
    dependencies = [
        ("socialmedia", "0001_initial"),
        ("core", "0007_case_search_indexes"),
    ]

    operations = [
        *search_indexes(
            "account", "socialmedia_socialaccount", ("platform", "username", "source")
        ),
    ]
