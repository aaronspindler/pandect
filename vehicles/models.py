from uuid import uuid4

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class Vehicle(models.Model):
    """An investigator-maintained vehicle record within a single case."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey(
        "cases.Case", on_delete=models.CASCADE, related_name="vehicles"
    )
    make = models.CharField(max_length=150, blank=True, default="")
    model = models.CharField(max_length=150, blank=True, default="")
    year = models.PositiveSmallIntegerField(
        blank=True,
        null=True,
        validators=[MinValueValidator(1), MaxValueValidator(9999)],
    )
    color = models.CharField(max_length=100, blank=True, default="")
    license_plate = models.CharField(max_length=64, blank=True, default="")
    vin = models.CharField("VIN", max_length=64, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("make", "model", "license_plate", "pk")
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(make__regex=r"\S")
                    | models.Q(model__regex=r"\S")
                    | models.Q(license_plate__regex=r"\S")
                    | models.Q(vin__regex=r"\S")
                ),
                name="vehicles_vehicle_identity_present",
                violation_error_message=(
                    "Enter at least a make, model, license plate, or VIN."
                ),
            ),
            models.CheckConstraint(
                condition=models.Q(year__isnull=True)
                | models.Q(year__gte=1, year__lte=9999),
                name="vehicles_vehicle_year_range",
                violation_error_message="Enter a year between 1 and 9999.",
            ),
        ]

    def __str__(self):
        name = " ".join(part.strip() for part in (self.make, self.model)).strip()
        if name:
            return f"{self.year} {name}" if self.year else name
        return self.license_plate.strip() or self.vin.strip()
