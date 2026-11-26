"""Case-local adjacency and provenance for reviewable narrative suggestions."""

from uuid import uuid4

from django.db import models


class NarrativeRelationship(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey("cases.Case", on_delete=models.CASCADE)
    result = models.ForeignKey("core.AnalysisResult", on_delete=models.CASCADE)
    item_index = models.PositiveIntegerField()
    fingerprint = models.CharField(max_length=64)
    evidence_id = models.UUIDField()
    subject = models.CharField(max_length=160)
    predicate = models.CharField(max_length=24)
    object = models.CharField(max_length=160)
    qualification = models.CharField(max_length=16)
    locator = models.JSONField()
    start = models.PositiveIntegerField()
    quote = models.CharField(max_length=1024)
    available_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["case", "fingerprint"], name="case_narrative_edge"
            )
        ]
        indexes = [
            models.Index(fields=["case", "evidence_id"], name="case_narrative_source")
        ]

    def __str__(self):
        return f"{self.subject} {self.predicate} {self.object}"
