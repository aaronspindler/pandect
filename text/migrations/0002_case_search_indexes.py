"""Indexed case search across native text records."""

from django.db import migrations

from core.migrations._case_search_indexes import EVIDENCE, search_indexes


class Migration(migrations.Migration):
    dependencies = [("text", "0001_initial"), ("core", "0007_case_search_indexes")]

    operations = [
        *search_indexes("text", "text_textevidence", EVIDENCE + ("body",)),
    ]
