"""Indexed case search across native document records."""

from django.db import migrations

from core.migrations._case_search_indexes import EVIDENCE, search_indexes


class Migration(migrations.Migration):
    dependencies = [("document", "0001_initial"), ("core", "0007_case_search_indexes")]

    operations = [
        *search_indexes(
            "document", "document_documentevidence", EVIDENCE + ("original_filename",)
        ),
    ]
