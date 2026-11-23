from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone


class Case(models.Model):
    """The root investigation record and boundary for future case material."""

    class AIBackend(models.TextChoices):
        LOCAL = "local", "Local AI"
        GATEWAY = "gateway", "External AI"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="owned_cases",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    name = models.CharField(max_length=255, default="")
    description = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    external_analysis_approval = models.JSONField(
        default=dict, blank=True, editable=False
    )
    external_analysis_models = models.JSONField(
        default=dict, db_default={}, blank=True, editable=False
    )
    search_embedding_approval = models.JSONField(
        default=dict, db_default={}, blank=True, editable=False
    )
    ai_backend = models.CharField(
        max_length=7,
        choices=AIBackend,
        default=AIBackend.LOCAL,
        db_default=AIBackend.LOCAL,
        editable=False,
    )
    additional_users = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        through="CaseMembership",
        related_name="shared_cases",
        blank=True,
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(name__regex=r"\S"),
                name="cases_case_name_present",
            ),
            models.CheckConstraint(
                condition=models.Q(ai_backend__in=("local", "gateway")),
                name="cases_valid_ai_backend",
            ),
        ]

    def __str__(self):
        return self.name or f"Case #{self.pk}"

    def save(self, *args, **kwargs):
        if self._state.adding and not self.name.strip():
            self.name = f"Case #{self.pk}"
        if kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = set(kwargs["update_fields"]) | {"updated_at"}
        super().save(*args, **kwargs)


class CaseActivity(models.Model):
    """Committed collection revisions. Domain writes advance these in PostgreSQL."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.OneToOneField(Case, related_name="activity", on_delete=models.CASCADE)
    work = models.BigIntegerField(db_default=0)
    evidence = models.BigIntegerField(db_default=0)
    images = models.BigIntegerField(db_default=0)
    findings = models.BigIntegerField(db_default=0)
    people = models.BigIntegerField(db_default=0)
    vehicles = models.BigIntegerField(db_default=0)
    important = models.BigIntegerField(db_default=0)

    def __str__(self):
        return f"Activity in case {self.case_id}"

    def versions(self):
        return {
            name: getattr(self, name)
            for name in (
                "work",
                "evidence",
                "images",
                "findings",
                "people",
                "vehicles",
                "important",
            )
        }


class CaseMembership(models.Model):
    class Role(models.TextChoices):
        VIEWER = "viewer", "Viewer"
        EDITOR = "editor", "Editor"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
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


class CaseInvitation(models.Model):
    """A revocable, email-bound invitation; raw tokens are never stored."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey(Case, on_delete=models.CASCADE, related_name="invitations")
    email = models.EmailField(max_length=254)
    role = models.CharField(
        max_length=6, choices=CaseMembership.Role, default=CaseMembership.Role.VIEWER
    )
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="case_invitations_sent",
    )
    token_digest = models.CharField(max_length=64, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="case_invitations_accepted",
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_sent_at = models.DateTimeField(null=True, blank=True)
    delivery_failed = models.BooleanField(default=False)

    class Meta:
        ordering = ("-created_at", "pk")
        constraints = [
            models.UniqueConstraint(
                Lower("email"),
                models.F("case"),
                condition=models.Q(accepted_at__isnull=True, revoked_at__isnull=True),
                name="cases_one_pending_invitation",
            ),
            models.CheckConstraint(
                condition=models.Q(role__in=("viewer", "editor")),
                name="cases_valid_invitation_role",
            ),
        ]

    def __str__(self):
        return f"{self.email} ({self.get_role_display()})"

    @property
    def status(self):
        if self.accepted_at:
            return "Accepted"
        if self.revoked_at:
            return "Revoked"
        if self.expires_at <= timezone.now():
            return "Expired"
        if self.delivery_failed:
            return "Delivery failed"
        return "Pending"

    @property
    def is_pending(self):
        return not self.accepted_at and not self.revoked_at
