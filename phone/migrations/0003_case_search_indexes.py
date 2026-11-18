"""Indexed case search across native phone records."""

from django.db import migrations

from core.migrations._case_search_indexes import EVIDENCE, search_indexes


class Migration(migrations.Migration):
    dependencies = [
        ("phone", "0002_communicationcontext_import_run"),
        ("core", "0007_case_search_indexes"),
    ]

    operations = [
        *search_indexes(
            "text-message", "phone_textmessageevidence", EVIDENCE + ("body",)
        ),
        *search_indexes("phone-call", "phone_phonecallevidence", EVIDENCE),
        *search_indexes("facetime", "phone_facetimeevidence", EVIDENCE),
        *search_indexes(
            "attachment",
            "phone_callevidenceattachment",
            ("original_filename", "transcript", "sha256", "audio_sha256"),
        ),
        *search_indexes(
            "context",
            "phone_communicationcontext",
            (
                "native_message_id",
                "native_thread_id",
                "reply_to_id",
                "raw_sender",
                "raw_recipient",
            ),
        ),
    ]
