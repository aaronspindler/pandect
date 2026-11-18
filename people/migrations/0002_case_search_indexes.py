"""Indexed case search across native people records."""

from django.db import migrations

from core.migrations._case_search_indexes import search_indexes


class Migration(migrations.Migration):
    dependencies = [("people", "0001_initial"), ("core", "0007_case_search_indexes")]

    operations = [
        *search_indexes(
            "person",
            "people_person",
            ("first_name", "last_name", "phone_number", "email", "address", "notes"),
        ),
    ]
