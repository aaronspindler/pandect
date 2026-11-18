"""Indexed case search across native core records."""

from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations

from core.migrations._case_search_indexes import search_indexes


class Migration(migrations.Migration):
    dependencies = [("core", "0006_timing_only_findings")]

    operations = [
        TrigramExtension(),
        *search_indexes("finding", "core_analysisfinding", ("search_text",)),
        *search_indexes("passage", "core_extractedpassage", ("text",)),
    ]
