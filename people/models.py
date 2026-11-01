from uuid import uuid4

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
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


class PointOfInterest(models.Model):
    """An editable investigator annotation, scoped through its required person."""

    class Kind(models.TextChoices):
        HOME = "home", "Home"
        WORK = "work", "Work"
        CUSTOM = "custom", "Custom"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    person = models.ForeignKey(
        Person, on_delete=models.CASCADE, related_name="points_of_interest"
    )
    kind = models.CharField(max_length=10, choices=Kind, default=Kind.HOME)
    custom_label = models.CharField(max_length=150, blank=True, default="")
    latitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        validators=[MinValueValidator(-90), MaxValueValidator(90)],
    )
    longitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        validators=[MinValueValidator(-180), MaxValueValidator(180)],
    )
    address = models.TextField(max_length=2000, blank=True, default="")
    source = models.CharField(max_length=255, blank=True, default="")
    notes = models.TextField(max_length=10000, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="points_of_interest",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at", "-pk")
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(kind__in=("home", "work"), custom_label="")
                    | models.Q(kind="custom", custom_label__regex=r"\S")
                ),
                name="people_poi_label_valid",
                violation_error_message="Choose Home, Work, or Custom with a label.",
            ),
            models.CheckConstraint(
                condition=models.Q(latitude__gte=-90, latitude__lte=90),
                name="people_poi_latitude_range",
            ),
            models.CheckConstraint(
                condition=models.Q(longitude__gte=-180, longitude__lte=180),
                name="people_poi_longitude_range",
            ),
        ]

    def __str__(self):
        return f"{self.label} · {self.person}"

    @property
    def label(self):
        return (
            self.custom_label
            if self.kind == self.Kind.CUSTOM
            else self.get_kind_display()
        )

    @property
    def plottable(self):
        return abs(self.latitude) <= 85.05112878
