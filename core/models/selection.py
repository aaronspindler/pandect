"""Named, case-shared references to exact retained sources and findings."""

import json
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class SavedSelection(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey("cases.Case", on_delete=models.CASCADE)
    name = models.CharField(max_length=160)
    members = models.JSONField(default=list, blank=True)
    revision = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_selections",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="updated_selections",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name", "pk")
        constraints = [
            models.CheckConstraint(
                condition=models.Q(name__regex=r"\S"), name="selection_name_present"
            ),
            models.CheckConstraint(
                condition=models.Q(revision__gte=1), name="selection_revision_positive"
            ),
        ]

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if not isinstance(self.members, list) or len(self.members) > 500:
            raise ValidationError("A selection supports at most 500 items.")
        if len(json.dumps(self.members).encode()) > 262144:
            raise ValidationError(
                "The selection references exceed the supported limit."
            )
        if (
            not self._state.adding
            and type(self)
            .objects.filter(pk=self.pk)
            .exclude(case_id=self.case_id)
            .exists()
        ):
            raise ValidationError("Selections cannot move between cases.")
