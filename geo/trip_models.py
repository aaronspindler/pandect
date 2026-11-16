"""Versioned vehicle trips, independent of disposable operational queue rows."""

from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class VehicleTripState(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey("cases.Case", on_delete=models.CASCADE)
    vehicle = models.OneToOneField("vehicle.Vehicle", on_delete=models.CASCADE)
    revision = models.PositiveBigIntegerField(default=0)
    current_generation = models.ForeignKey(
        "geo.VehicleTripGeneration",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="published_states",
    )

    def __str__(self):
        return f"Vehicle trip state {self.pk}"


class VehicleTripGeneration(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Processing trips"
        COMPLETE = "complete", "Complete"
        FAILED = "failed", "Needs attention"
        SUPERSEDED = "superseded", "Superseded"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey("cases.Case", on_delete=models.CASCADE)
    vehicle = models.ForeignKey("vehicle.Vehicle", on_delete=models.CASCADE)
    state = models.ForeignKey(
        VehicleTripState, on_delete=models.CASCADE, related_name="generations"
    )
    revision = models.PositiveBigIntegerField()
    method_version = models.CharField(max_length=64)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    previous_generation = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL
    )
    status = models.CharField(max_length=16, choices=Status, default=Status.QUEUED)
    token = models.UUIDField(default=uuid4, editable=False)
    attempts = models.PositiveSmallIntegerField(default=1)
    total_sources = models.PositiveBigIntegerField(default=0)
    processed_sources = models.PositiveBigIntegerField(default=0)
    cursor = models.JSONField(default=dict)
    carry = models.JSONField(default=dict)
    metrics = models.JSONField(default=dict)
    error_code = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    geocode_cursor = models.PositiveBigIntegerField(default=0)
    geocode_status = models.CharField(max_length=16, default="pending")
    geocode_attempts = models.PositiveSmallIntegerField(default=0)
    geocode_requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="prepared_trip_addresses",
    )
    geocode_error_code = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ("-created_at", "pk")
        constraints = [
            models.CheckConstraint(
                condition=models.Q(attempts__gte=1, attempts__lte=3),
                name="geo_trip_attempt_bound",
            ),
            models.CheckConstraint(
                condition=models.Q(geocode_attempts__lte=3),
                name="geo_trip_geocode_attempt_bound",
            ),
        ]
        indexes = [models.Index(fields=("state", "revision", "status"))]

    def __str__(self):
        return f"Vehicle trip generation {self.pk}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            prior = type(self).objects.get(pk=self.pk)
            fields = (
                "case_id",
                "vehicle_id",
                "state_id",
                "revision",
                "method_version",
                "requested_by_id",
                "previous_generation_id",
            )
            if any(getattr(self, field) != getattr(prior, field) for field in fields):
                raise ValidationError(
                    "Trip inputs and processing attribution are retained. "
                    "Prepare another generation."
                )
        if self._state.adding:
            kwargs["force_insert"] = True
        return super().save(*args, **kwargs)


class VehicleTrip(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey("cases.Case", on_delete=models.CASCADE)
    vehicle = models.ForeignKey("vehicle.Vehicle", on_delete=models.CASCADE)
    generation = models.ForeignKey(
        VehicleTripGeneration, on_delete=models.CASCADE, related_name="trips"
    )
    evidence_state = models.CharField(
        max_length=8,
        choices=[("active", "Active"), ("archived", "Archived"), ("all", "All")],
    )
    kind = models.CharField(
        max_length=4,
        choices=[("trip", "Trip"), ("gap", "Coverage gap")],
        default="trip",
    )
    ordinal = models.PositiveBigIntegerField()
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    source_count = models.PositiveBigIntegerField(default=0)
    largest_gap_seconds = models.FloatField(default=0)
    basis = models.CharField(max_length=80, blank=True)

    class Meta:
        ordering = ("-start_at", "pk")
        constraints = [
            models.UniqueConstraint(
                fields=("generation", "evidence_state", "ordinal"),
                name="geo_trip_generation_order",
            ),
            models.CheckConstraint(
                condition=models.Q(end_at__gte=models.F("start_at")),
                name="geo_trip_time_order",
            ),
        ]
        indexes = [models.Index(fields=("case", "evidence_state", "kind", "start_at"))]

    def __str__(self):
        return f"Vehicle trip {self.pk}"

    def save(self, *args, **kwargs):
        if (
            not self._state.adding
            and VehicleTripGeneration.objects.filter(
                pk=self.generation_id, status="complete"
            ).exists()
        ):
            raise ValidationError("Published trips cannot be replaced.")
        return super().save(*args, **kwargs)


class VehicleTripInput(models.Model):
    """Every processed original, including stationary and untimed observations."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey("cases.Case", on_delete=models.CASCADE)
    generation = models.ForeignKey(
        VehicleTripGeneration, on_delete=models.CASCADE, related_name="inputs"
    )
    observation = models.ForeignKey(
        "geo.VehicleLocationEvidence",
        on_delete=models.RESTRICT,
        related_name="trip_inputs",
    )
    ordinal = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)
    snapshot = models.JSONField()

    class Meta:
        ordering = ("ordinal",)
        constraints = [
            models.UniqueConstraint(
                fields=("generation", "ordinal"), name="geo_trip_input_order"
            ),
            models.UniqueConstraint(
                fields=("generation", "observation"), name="geo_trip_input_original"
            ),
        ]

    def __str__(self):
        return f"Vehicle trip input {self.pk}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Trip source snapshots cannot be replaced.")
        kwargs["force_insert"] = True
        return super().save(*args, **kwargs)


class VehicleTripSource(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey("cases.Case", on_delete=models.CASCADE)
    trip = models.ForeignKey(
        VehicleTrip, on_delete=models.CASCADE, related_name="sources"
    )
    input = models.ForeignKey(
        VehicleTripInput, on_delete=models.CASCADE, related_name="memberships"
    )
    ordinal = models.PositiveBigIntegerField()
    gap_seconds = models.FloatField(default=0)

    class Meta:
        ordering = ("ordinal",)
        constraints = [
            models.UniqueConstraint(
                fields=("trip", "ordinal"), name="geo_trip_source_order"
            )
        ]

    def __str__(self):
        return f"Vehicle trip source {self.pk}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Trip source membership cannot be replaced.")
        kwargs["force_insert"] = True
        return super().save(*args, **kwargs)
