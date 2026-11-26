import hashlib
from datetime import UTC
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from core.models import Evidence
from core.storage import (
    canonical_bytes,
)
from document.models import DocumentEvidence
from geo.coordinates import WEB_MERCATOR_MAX_LATITUDE
from geo.trip_models import (  # noqa: F401
    VehicleTrip,
    VehicleTripGeneration,
    VehicleTripInput,
    VehicleTripSource,
    VehicleTripState,
)
from geo.vehicle_imports import PARSER_VERSION


class LocationContent(Evidence):
    """An immutable, investigator-supplied location observation."""

    address = models.TextField(blank=True, max_length=20000)
    latitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
        validators=[MinValueValidator(-90), MaxValueValidator(90)],
    )
    longitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
        validators=[MinValueValidator(-180), MaxValueValidator(180)],
    )
    observed_at = models.DateTimeField(null=True, blank=True)

    immutable_fields = (
        *Evidence.immutable_fields,
        "address",
        "latitude",
        "longitude",
        "observed_at",
    )

    class Meta(Evidence.Meta):
        abstract = True
        constraints = [
            *Evidence.Meta.constraints,
            models.CheckConstraint(
                condition=models.Q(address__regex=r"\S")
                | models.Q(latitude__isnull=False, longitude__isnull=False),
                name="%(app_label)s_%(class)s_location_present",
            ),
            models.CheckConstraint(
                condition=models.Q(latitude__isnull=True, longitude__isnull=True)
                | models.Q(latitude__isnull=False, longitude__isnull=False),
                name="%(app_label)s_%(class)s_coordinate_pair",
            ),
            models.CheckConstraint(
                condition=models.Q(latitude__isnull=True)
                | models.Q(latitude__gte=-90, latitude__lte=90),
                name="%(app_label)s_%(class)s_latitude_range",
            ),
            models.CheckConstraint(
                condition=models.Q(longitude__isnull=True)
                | models.Q(longitude__gte=-180, longitude__lte=180),
                name="%(app_label)s_%(class)s_longitude_range",
            ),
        ]

    def original_data(self):
        """Versioned JSON values shared by hashing and private downloads."""
        return {
            "schema": "pandect.location.v1",
            "address": self.address,
            "latitude": (
                format(self.latitude or 0, ".6f") if self.latitude is not None else None
            ),
            "longitude": (
                format(self.longitude or 0, ".6f")
                if self.longitude is not None
                else None
            ),
            "observed_at": (
                self.observed_at.astimezone(UTC).isoformat()
                if self.observed_at is not None
                else None
            ),
        }

    def original_bytes(self):
        return canonical_bytes(self.original_data())

    def _prepare_original(self):
        for name in ("address", "latitude", "longitude", "observed_at"):
            field = self._meta.get_field(name)
            setattr(self, name, field.clean(getattr(self, name), self))
        if (self.latitude is None) != (self.longitude is None):
            raise ValidationError("Enter both latitude and longitude, or neither.")
        if not self.address.strip() and self.latitude is None:
            raise ValidationError("Enter an address or both coordinates.")
        if self.observed_at is not None and timezone.is_naive(self.observed_at):
            self.observed_at = timezone.make_aware(self.observed_at)
        self.sha256 = hashlib.sha256(self.original_bytes()).hexdigest()


class LocationEvidence(LocationContent):
    class Meta(LocationContent.Meta):
        indexes = [
            models.Index(
                fields=("case", "observed_at", "id"),
                condition=models.Q(is_archived=False, observed_at__isnull=False),
                name="geo_location_case_time",
            ),
        ]


class PersonLocationEvidence(LocationContent):
    """A reported association with a case person, not proof of presence."""

    person = models.ForeignKey(
        "people.Person", on_delete=models.RESTRICT, related_name="location_evidence"
    )

    immutable_fields = (*LocationContent.immutable_fields, "person_id")

    class Meta(LocationContent.Meta):
        indexes = [
            models.Index(
                fields=("case", "observed_at", "id"),
                condition=models.Q(is_archived=False, observed_at__isnull=False),
                name="geo_person_loc_case_time",
            ),
        ]

    def _check_original(self, using):
        super()._check_original(using)
        from people.models import Person

        if (
            not self.person_id
            or not Person.objects.using(using)
            .filter(pk=self.person_id, case_id=self.case_id)
            .exists()
        ):
            raise ValidationError({"person": "Choose a person in this case."})

    def original_data(self):
        return {
            **super().original_data(),
            "schema": "pandect.person-location.v1",
            "person_id": str(self.person_id),
        }


class VehicleLocationImport(models.Model):
    """A retained CSV and the interpretation used for one atomic import."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey("cases.Case", on_delete=models.CASCADE)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    submission_id = models.UUIDField(null=True, blank=True, editable=False)
    document = models.OneToOneField(DocumentEvidence, on_delete=models.RESTRICT)
    timezone = models.CharField(max_length=100)
    parser_version = models.CharField(max_length=50, default=PARSER_VERSION)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    vehicle_mapping = models.JSONField(default=dict, blank=True)
    selected_vehicle = models.ForeignKey(
        "vehicle.Vehicle",
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="location_imports",
    )
    status = models.CharField(
        max_length=20,
        default="pending",
        choices=[
            ("pending", "Awaiting preparation"),
            ("queued", "Queued"),
            ("running", "Processing"),
            ("review", "Ready to review"),
            ("complete", "Complete"),
            ("failed", "Needs attention"),
        ],
    )
    phase = models.CharField(max_length=10, default="prepare")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="requested_vehicle_imports",
    )
    job_token = models.UUIDField(default=uuid4, editable=False)
    attempts = models.PositiveSmallIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    row_count = models.PositiveIntegerField(default=0)
    processed_rows = models.PositiveIntegerField(default=0)
    invalid_count = models.PositiveIntegerField(default=0)
    duplicate_count = models.PositiveIntegerField(default=0)
    groups = models.JSONField(default=list, blank=True)
    error_code = models.CharField(max_length=50, blank=True)
    duplicates_confirmed = models.BooleanField(default=False)

    class Meta:
        ordering = ("-created_at", "-pk")
        constraints = [
            models.UniqueConstraint(
                fields=("case", "uploaded_by", "submission_id"),
                name="vehicle_import_submission_scope",
            ),
            models.CheckConstraint(
                condition=models.Q(attempts__lte=3),
                name="evidence_vehicle_import_attempt_bound",
            ),
        ]

    def __str__(self):
        return f"Vehicle location import {self.pk}"

    def save(self, *args, **kwargs):
        self.clean()
        if self._state.adding:
            kwargs["force_insert"] = True
        else:
            kwargs["force_update"] = True
        return super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        from vehicle.models import Vehicle

        if (
            self.selected_vehicle_id
            and not Vehicle.objects.filter(
                pk=self.selected_vehicle_id, case_id=self.case_id
            ).exists()
        ):
            raise ValidationError("Choose a vehicle in this case.")
        if not DocumentEvidence.objects.filter(
            pk=self.document_id,
            case_id=self.case_id,
            uploaded_by_id=self.uploaded_by_id,
        ).exists():
            raise ValidationError("Choose a source document uploaded in this case.")
        if not self._state.adding:
            original = type(self).objects.filter(pk=self.pk).values().first()
            if original is None:
                raise ValidationError("This import no longer exists.")
            immutable = (
                "case_id",
                "uploaded_by_id",
                "document_id",
                "submission_id",
                "timezone",
                "parser_version",
                "created_at",
                "selected_vehicle_id",
            )
            if original["completed_at"] is not None:
                immutable += (
                    "completed_at",
                    "vehicle_mapping",
                    "duplicates_confirmed",
                    "duplicate_count",
                )
            if any(getattr(self, name) != original[name] for name in immutable):
                raise ValidationError(
                    "The retained import interpretation cannot change."
                )


class VehicleLocationImportRow(models.Model):
    """Private, pageable interpretation of a retained import's original records."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    batch = models.ForeignKey(
        VehicleLocationImport, on_delete=models.CASCADE, related_name="rows"
    )
    number = models.PositiveIntegerField()
    group_index = models.PositiveSmallIntegerField()
    raw = models.JSONField(default=dict)
    observed_at = models.DateTimeField(null=True)
    received_at = models.DateTimeField(null=True)
    speed_kmh = models.DecimalField(max_digits=9, decimal_places=3, null=True)
    errors = models.JSONField(default=list)
    duplicate = models.BooleanField(default=False)
    fingerprint = models.CharField(max_length=64)

    class Meta:
        ordering = ("number",)
        constraints = [
            models.UniqueConstraint(
                fields=("batch", "number"), name="evidence_vehicle_stage_row"
            ),
        ]
        indexes = [models.Index(fields=("batch", "fingerprint"))]

    def __str__(self):
        return f"Vehicle location import {self.batch_id}, row {self.number}"

    @property
    def asset(self):
        return self.raw.get("Asset", "")

    @property
    def serial(self):
        return self.raw.get("Serial Number", "")

    @property
    def values(self):
        return {
            "address": self.raw.get("Address", ""),
            "observed_at": self.observed_at,
            "received_at": self.received_at,
            "speed_kmh": self.speed_kmh,
        }


class VehicleLocationEvidence(LocationContent):
    """A reported vehicle association, optionally extracted from a retained CSV."""

    vehicle = models.ForeignKey(
        "vehicle.Vehicle", on_delete=models.RESTRICT, related_name="location_evidence"
    )
    import_batch = models.ForeignKey(
        VehicleLocationImport,
        on_delete=models.RESTRICT,
        related_name="observations",
        null=True,
        blank=True,
    )
    source_row = models.PositiveIntegerField(null=True, blank=True)
    raw_row = models.JSONField(default=dict, blank=True)
    raw_fingerprint = models.CharField(max_length=64, blank=True, editable=False)
    speed_kmh = models.DecimalField(
        max_digits=9,
        decimal_places=3,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
    )
    received_at = models.DateTimeField(null=True, blank=True)

    immutable_fields = (
        *LocationContent.immutable_fields,
        "vehicle_id",
        "import_batch_id",
        "source_row",
        "raw_row",
        "raw_fingerprint",
        "speed_kmh",
        "received_at",
    )

    class Meta(LocationContent.Meta):
        indexes = [
            models.Index(fields=("case", "raw_fingerprint")),
            models.Index(fields=("case", "vehicle", "observed_at", "id")),
            models.Index(
                fields=("case", "observed_at", "id"),
                condition=models.Q(is_archived=False, observed_at__isnull=False),
                name="geo_vehicle_loc_case_time",
            ),
        ]
        constraints = [
            *LocationContent.Meta.constraints,
            models.UniqueConstraint(
                fields=("import_batch", "source_row"),
                name="evidence_vehicle_import_row",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(import_batch__isnull=True, source_row__isnull=True)
                    | models.Q(
                        import_batch__isnull=False,
                        source_row__isnull=False,
                        source_row__gte=2,
                    )
                ),
                name="evidence_vehicle_import_source_pair",
            ),
            models.CheckConstraint(
                condition=models.Q(speed_kmh__isnull=True) | models.Q(speed_kmh__gte=0),
                name="evidence_vehicle_speed_nonnegative",
            ),
        ]

    def _check_original(self, using):
        super()._check_original(using)
        from vehicle.models import Vehicle

        if (
            not Vehicle.objects.using(using)
            .filter(pk=self.vehicle_id, case_id=self.case_id)
            .exists()
        ):
            raise ValidationError({"vehicle": "Choose a vehicle in this case."})
        if (
            self.import_batch_id
            and not VehicleLocationImport.objects.using(using)
            .filter(
                pk=self.import_batch_id,
                case_id=self.case_id,
                document__case_id=self.case_id,
            )
            .exists()
        ):
            raise ValidationError("Choose an import in this case.")

    def _prepare_original(self):
        from geo.vehicle_imports import row_fingerprint

        self.raw_fingerprint = (
            row_fingerprint(self.raw_row) if self.import_batch_id else ""
        )
        for name in ("speed_kmh", "received_at", "source_row", "raw_row"):
            field = self._meta.get_field(name)
            setattr(self, name, field.clean(getattr(self, name), self))
        if self.speed_kmh == 0:
            # PostgreSQL numeric discards a zero's sign; hash its retained form.
            self.speed_kmh = abs(self.speed_kmh)
        if (self.import_batch_id is None) != (self.source_row is None):
            raise ValidationError("An imported observation requires its source row.")
        if self.received_at is not None and timezone.is_naive(self.received_at):
            raise ValidationError("The source receipt time requires a timezone.")
        super()._prepare_original()

    def original_data(self):
        provenance = None
        if self.import_batch_id:
            batch = self.import_batch
            provenance = {
                "import_id": str(batch.pk),
                "document_id": str(batch.document_id),
                "document_sha256": batch.document.sha256,
                "row": self.source_row,
                "timezone": batch.timezone,
                "parser_version": batch.parser_version,
            }
        return {
            **super().original_data(),
            "schema": "pandect.vehicle-location.v1",
            "vehicle_id": str(self.vehicle_id),
            "import": provenance,
            "raw_row": self.raw_row,
            "speed_kmh": format(self.speed_kmh, ".3f")
            if self.speed_kmh is not None
            else None,
            "received_at": self.received_at.astimezone(UTC).isoformat()
            if self.received_at
            else None,
        }


class PointOfInterest(models.Model):
    """An editable investigator annotation, scoped through its required person."""

    class Kind(models.TextChoices):
        HOME = "home", "Home"
        WORK = "work", "Work"
        CUSTOM = "custom", "Custom"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    person = models.ForeignKey(
        "people.Person", on_delete=models.CASCADE, related_name="points_of_interest"
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
        return abs(self.latitude) <= WEB_MERCATOR_MAX_LATITUDE
