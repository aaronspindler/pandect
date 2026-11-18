"""Frozen native search index definitions shared by the November 2026 migrations."""

from django.db import migrations

EVIDENCE = (
    "title",
    "source",
    "source_url",
    "description",
    "source_description",
    "sha256",
)


def search_indexes(kind, table, fields):
    document = " || ' ' || ".join(f'"{field}"' for field in fields)
    vector = (
        f"to_tsvector('simple'::regconfig, COALESCE(LEFT(({document}), 32000), ''))"
    )
    definitions = {
        "words": ("gin", vector),
        "string": ("gin", f"upper(({document})) gin_trgm_ops"),
    }
    if "title" in fields:
        definitions.update(
            title=("gin", "upper(title) gin_trgm_ops"),
            sha=("btree", "upper(sha256) text_pattern_ops"),
        )
        if "original_filename" in fields:
            definitions["file"] = ("btree", "upper(original_filename) text_pattern_ops")
    operations = []
    for suffix, (method, expression) in definitions.items():
        name = f"srch_{kind.replace('-', '_')}_{suffix}"
        operations.append(
            migrations.RunSQL(
                f'CREATE INDEX "{name}" ON "{table}" USING {method} ({expression})',
                f'DROP INDEX "{name}"',
            )
        )
    return operations
