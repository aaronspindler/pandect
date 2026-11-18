"""Native field expressions used by case search and stored PostgreSQL vectors."""

from django.contrib.postgres.search import SearchVector
from django.db.models import F, Func, TextField
from django.db.models.functions import Left

# Bound tsvector size on long, unique text. Substring matching covers the tail
# and punctuation-bearing literals through a separate trigram index.
VECTOR_CHARS = 32_000
COMMON = (
    "title",
    "source",
    "source_url",
    "description",
    "source_description",
    "sha256",
)


def search_document(fields):
    if len(fields) == 1:
        return F(fields[0])
    return Func(
        *fields,
        template="(%(expressions)s)",
        arg_joiner=" || ' ' || ",
        output_field=TextField(),
    )


def word_vector(document):
    return SearchVector(Left(document, VECTOR_CHARS), config="simple")
