"""Rebuildable private passage index and its durable, case-scoped work cursor."""

from uuid import uuid4

from django.conf import settings
from django.db import models
from django.db.models.lookups import In
from pgvector.django import VectorField


class CaseSearchIndex(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.OneToOneField("cases.Case", on_delete=models.CASCADE)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    profile = models.CharField(max_length=64)
    execution = models.JSONField(default=dict)
    status = models.CharField(max_length=12, default="queued")
    token = models.UUIDField(default=uuid4)
    source_type_index = models.PositiveSmallIntegerField(default=0)
    source_cursor = models.UUIDField(null=True)
    failures = models.PositiveSmallIntegerField(default=0)
    error_code = models.CharField(max_length=64, blank=True)

    def __str__(self):
        return f"Search index for case {self.case_id}"


SOURCE_FIELDS = ("text", "message", "attachment", "extraction", "finding")


class SearchDocument(models.Model):
    """One immutable passage source. Foreign keys also bind deletion inventories."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey("cases.Case", on_delete=models.CASCADE)
    text = models.OneToOneField(
        "text.TextEvidence", on_delete=models.CASCADE, null=True
    )
    message = models.OneToOneField(
        "phone.TextMessageEvidence", on_delete=models.CASCADE, null=True
    )
    attachment = models.OneToOneField(
        "phone.CallEvidenceAttachment", on_delete=models.CASCADE, null=True
    )
    extraction = models.OneToOneField(
        "core.ExtractedPassage", on_delete=models.CASCADE, null=True
    )
    finding = models.OneToOneField(
        "core.AnalysisFinding", on_delete=models.CASCADE, null=True
    )
    # A profile includes model digest, runtime and chunking version. A partial
    # document remains private until its last chunk has committed successfully.
    profile = models.CharField(max_length=64, blank=True)
    offset = models.PositiveIntegerField(default=0)
    complete = models.BooleanField(default=False)

    class Meta:
        indexes = [
            models.Index(
                fields=["case", "id"],
                condition=models.Q(complete=False),
                name="search_pending_documents",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    *(
                        models.Q(
                            **{
                                f"{name}__isnull": name != selected
                                for name in SOURCE_FIELDS
                            }
                        )
                        for selected in SOURCE_FIELDS
                    ),
                    _connector=models.Q.OR,
                ),
                name="case_search_one_passage_source",
            )
        ]

    def __str__(self):
        return f"Indexed source {self.pk}"

    def source(self):
        return next(
            (key, source_id)
            for key in SOURCE_FIELDS
            if (source_id := getattr(self, f"{key}_id"))
        )


class SearchPassage(models.Model):
    """Source offsets and fingerprint are sufficient to reproduce the excerpt."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey("cases.Case", on_delete=models.CASCADE)
    document = models.ForeignKey(SearchDocument, on_delete=models.CASCADE)
    start = models.PositiveIntegerField()
    length = models.PositiveSmallIntegerField()
    digest = models.CharField(max_length=64)
    profile = models.CharField(max_length=64)
    embedding = VectorField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["document", "profile", "start"],
                name="case_search_profile_offset",
            ),
            models.CheckConstraint(
                condition=In(
                    models.Func(
                        "embedding",
                        function="vector_dims",
                        output_field=models.IntegerField(),
                    ),
                    (384, 1536),
                ),
                name="case_search_embedding_dimensions",
            ),
        ]

    def __str__(self):
        return f"Passage at {self.start} in {self.document_id}"
