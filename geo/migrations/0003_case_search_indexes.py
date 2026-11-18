"""Indexed case search across native geo records."""

from django.db import migrations

from core.migrations._case_search_indexes import EVIDENCE, search_indexes


class Migration(migrations.Migration):
    dependencies = [
        ("geo", "0002_saved_vehicle_trips"),
        ("core", "0007_case_search_indexes"),
    ]

    operations = [
        *search_indexes("location", "geo_locationevidence", EVIDENCE + ("address",)),
        *search_indexes(
            "person-location", "geo_personlocationevidence", EVIDENCE + ("address",)
        ),
        *search_indexes(
            "vehicle-location", "geo_vehiclelocationevidence", EVIDENCE + ("address",)
        ),
        *search_indexes(
            "poi",
            "geo_pointofinterest",
            ("custom_label", "kind", "address", "source", "notes"),
        ),
        *search_indexes("trip", "geo_vehicletrip", ("basis",)),
    ]
