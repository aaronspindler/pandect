from uuid import uuid4

from django.conf import settings
from django.db import models


class ImportantItem(models.Model):
    """A case annotation on one exact evidence record or retained finding."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey("cases.Case", on_delete=models.CASCADE)
    evidence_kind = models.CharField(max_length=32, blank=True, default="")
    evidence_id = models.UUIDField(null=True, blank=True)
    finding_result = models.ForeignKey(
        "core.AnalysisResult", on_delete=models.CASCADE, null=True, blank=True
    )
    chat_turn = models.OneToOneField(
        "core.ChatTurn", on_delete=models.CASCADE, null=True, blank=True
    )
    item_index = models.PositiveIntegerField(null=True, blank=True)
    marked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True
    )
    marked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    ~models.Q(evidence_kind="")
                    & models.Q(
                        evidence_id__isnull=False,
                        finding_result__isnull=True,
                        item_index__isnull=True,
                        chat_turn__isnull=True,
                    )
                )
                | models.Q(
                    evidence_kind="",
                    evidence_id__isnull=True,
                    finding_result__isnull=False,
                    item_index__isnull=False,
                    chat_turn__isnull=True,
                )
                | models.Q(
                    evidence_kind="",
                    evidence_id__isnull=True,
                    finding_result__isnull=True,
                    item_index__isnull=True,
                    chat_turn__isnull=False,
                ),
                name="important_item_one_target",
            ),
            models.CheckConstraint(
                condition=models.Q(marked_at__isnull=True, marked_by__isnull=True)
                | models.Q(marked_at__isnull=False, marked_by__isnull=False),
                name="important_item_mark_attribution",
            ),
            models.UniqueConstraint(
                fields=("evidence_kind", "evidence_id"),
                condition=models.Q(evidence_id__isnull=False),
                name="important_item_unique_evidence",
            ),
            models.UniqueConstraint(
                fields=("finding_result", "item_index"),
                condition=models.Q(finding_result__isnull=False),
                name="important_item_unique_finding",
            ),
        ]
        indexes = [
            models.Index(
                fields=("case", "-marked_at", "-id"),
                condition=models.Q(marked_at__isnull=False),
                name="important_case_marked_idx",
            )
        ]

    def __str__(self):
        return f"Important item {self.pk}"


class ImportantNote(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    item = models.ForeignKey(
        ImportantItem, on_delete=models.CASCADE, related_name="notes"
    )
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    body = models.TextField(max_length=4000)

    class Meta:
        ordering = ("-created_at", "-pk")
        constraints = [
            models.CheckConstraint(
                condition=models.Q(body__regex=r"\S"),
                name="important_note_body_present",
            )
        ]

    def __str__(self):
        return f"Important item note {self.pk}"
