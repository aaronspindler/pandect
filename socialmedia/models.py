from uuid import uuid4

from django.db import models


class SocialAccount(models.Model):
    """A reported platform and username associated with a person."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    person = models.ForeignKey(
        "people.Person", on_delete=models.CASCADE, related_name="social_accounts"
    )
    platform = models.CharField(max_length=80)
    username = models.CharField(max_length=150)
    source = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("platform", "username", "pk")
        constraints = [
            models.CheckConstraint(
                condition=models.Q(platform__regex=r"\S", username__regex=r"\S"),
                name="people_social_account_details_present",
            ),
            models.UniqueConstraint(
                fields=("person", "platform", "username"),
                name="people_social_account_unique",
            ),
        ]

    def __str__(self):
        return f"{self.platform} · {self.username}"
