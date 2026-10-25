from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class Case(models.Model):
    """The root investigation record and boundary for future case material."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="owned_cases",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    additional_users = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        through="CaseMembership",
        related_name="shared_cases",
        blank=True,
    )

    def __str__(self):
        return f"Case #{self.pk}"


class CaseMembership(models.Model):
    class Role(models.TextChoices):
        VIEWER = "viewer", "Viewer"
        EDITOR = "editor", "Editor"

    case = models.ForeignKey(Case, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="case_memberships",
    )
    role = models.CharField(max_length=6, choices=Role, default=Role.VIEWER)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("case", "user"), name="cases_unique_membership"
            ),
            models.CheckConstraint(
                condition=models.Q(role__in=("viewer", "editor")),
                name="cases_valid_membership_role",
            ),
        ]

    def __str__(self):
        return f"{self.user} ({self.get_role_display()})"

    def clean(self):
        super().clean()
        if (
            self.case_id
            and self.user_id
            and Case.objects.filter(pk=self.case_id, owner_id=self.user_id).exists()
        ):
            raise ValidationError(
                {"user": "The owner already has access to this case."}
            )
