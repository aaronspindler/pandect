"""Rebuildable local routing records for source-grounded retrieval."""

from uuid import uuid4

from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVector, SearchVectorField
from django.db import models
from pgvector.django import VectorField

PARENT_ORIGINS = ("text", "attachment", "extraction", "summary", "session")


class SearchParent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey("cases.Case", on_delete=models.CASCADE)
    text = models.ForeignKey("text.TextEvidence", on_delete=models.CASCADE, null=True)
    attachment = models.ForeignKey(
        "phone.CallEvidenceAttachment", on_delete=models.CASCADE, null=True
    )
    extraction = models.ForeignKey(
        "core.ExtractedPassage", on_delete=models.CASCADE, null=True
    )
    summary = models.ForeignKey(
        "core.AnalysisResult", on_delete=models.CASCADE, null=True
    )
    session = models.ForeignKey(
        "phone.ConversationSession", on_delete=models.CASCADE, null=True
    )
    version = models.PositiveSmallIntegerField()
    label = models.CharField(max_length=32)
    outline = models.TextField()
    words = models.GeneratedField(
        expression=SearchVector("outline", config="simple"),
        output_field=SearchVectorField(),
        db_persist=True,
    )
    # Each anchor names a bounded verbatim span. Search never displays the outline.
    anchors = models.JSONField(default=list)
    coverage = models.JSONField(default=dict)
    vector_sources = ArrayField(models.CharField(max_length=80), default=list)
    embedding = VectorField(null=True)
    profile = models.CharField(max_length=64, blank=True)

    class Meta:
        indexes = [GinIndex(fields=["words"]), GinIndex(fields=["vector_sources"])]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    *(
                        models.Q(
                            **{
                                f"{field}__isnull": field != origin
                                for field in PARENT_ORIGINS
                            }
                        )
                        for origin in PARENT_ORIGINS
                    ),
                    _connector=models.Q.OR,
                ),
                name="search_parent_one_origin",
            )
        ]

    def __str__(self):
        return f"{self.label} for case {self.case_id}"


class SearchParentScan(models.Model):
    """Durable case backfill cursor; normal ingestion registers its own changes."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.OneToOneField("cases.Case", on_delete=models.CASCADE)
    version = models.PositiveSmallIntegerField()
    phase = models.PositiveSmallIntegerField(default=0)
    cursor = models.UUIDField(null=True)

    def __str__(self):
        return f"Outline scan for case {self.case_id}"
