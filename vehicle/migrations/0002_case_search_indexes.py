"""Indexed case search across native vehicle records."""

from django.db import migrations

from core.migrations._case_search_indexes import search_indexes


class Migration(migrations.Migration):
    dependencies = [("vehicle", "0001_initial"), ("core", "0007_case_search_indexes")]

    operations = [
        *search_indexes(
            "vehicle",
            "vehicle_vehicle",
            ("make", "model", "license_plate", "vin", "color", "notes"),
        ),
    ]
