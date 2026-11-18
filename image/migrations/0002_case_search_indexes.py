"""Indexed case search across native image records."""

from django.db import migrations

from core.migrations._case_search_indexes import EVIDENCE, search_indexes


class Migration(migrations.Migration):
    dependencies = [("image", "0001_initial"), ("core", "0007_case_search_indexes")]

    operations = [
        *search_indexes(
            "image", "image_imageevidence", EVIDENCE + ("original_filename",)
        ),
    ]
