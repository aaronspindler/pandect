from uuid import uuid4

from django.db import models


class Person(models.Model):
    """An investigator-maintained person record within a single case."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey(
        "cases.Case", on_delete=models.CASCADE, related_name="people"
    )
    first_name = models.CharField(max_length=150, blank=True, default="")
    last_name = models.CharField(max_length=150, blank=True, default="")
    phone_number = models.CharField(max_length=64, blank=True, default="")
    email = models.EmailField(max_length=254, blank=True, default="")
    address = models.TextField(blank=True, default="")
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "people"
        ordering = ("last_name", "first_name", "pk")
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(first_name__regex=r"\S")
                    | models.Q(last_name__regex=r"\S")
                    | models.Q(phone_number__regex=r"\S")
                    | models.Q(email__regex=r"\S")
                ),
                name="people_person_identity_present",
                violation_error_message=(
                    "Enter at least a first name, last name, phone number, or email."
                ),
            ),
        ]

    def __str__(self):
        name = " ".join(part.strip() for part in (self.first_name, self.last_name))
        return name.strip() or self.email or self.phone_number
