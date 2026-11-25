from django.contrib import admin
from django.db.models import F, Q
from django.urls import reverse
from django.utils.html import format_html

from cases.admin_access import CaseScopedReadOnlyAdmin
from core.evidence.admin import EvidenceAdmin
from geo.forms import (
    LocationEvidenceForm,
    PersonLocationEvidenceForm,
    VehicleLocationEvidenceForm,
)
from geo.models import (
    LocationEvidence,
    PersonLocationEvidence,
    PointOfInterest,
    VehicleLocationEvidence,
    VehicleLocationImport,
)
from geo.trip_models import VehicleTrip, VehicleTripGeneration, VehicleTripState
from geo.trip_processing import invalidate_vehicle_trips, saved_trips


@admin.register(LocationEvidence)
class LocationEvidenceAdmin(EvidenceAdmin):
    kind = "location"
    original_fields = LocationEvidenceForm.original_fields
    type_fields = LocationEvidenceForm.type_fields


@admin.register(PersonLocationEvidence)
class PersonLocationEvidenceAdmin(EvidenceAdmin):
    kind = "person-location"
    original_fields = PersonLocationEvidenceForm.original_fields
    type_fields = PersonLocationEvidenceForm.type_fields


@admin.register(VehicleLocationEvidence)
class VehicleLocationEvidenceAdmin(EvidenceAdmin):
    kind = "vehicle-location"
    original_fields = VehicleLocationEvidenceForm.original_fields
    type_fields = VehicleLocationEvidenceForm.type_fields
    import_fields = (
        "import_batch",
        "source_row",
        "raw_row",
        "speed_kmh",
        "received_at",
    )

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if not change:
            invalidate_vehicle_trips(request.user, obj.case, [obj.vehicle_id])


@admin.register(PointOfInterest)
class PointOfInterestAdmin(CaseScopedReadOnlyAdmin):
    case_lookup = "person__case"
    list_display = ("label", "person", "case_name", "kind", "updated_at")
    list_filter = ("kind",)
    search_fields = (
        "=id",
        "custom_label",
        "person__first_name",
        "person__last_name",
        "person__case__name",
    )
    fields = (
        "id",
        "case_name",
        "person",
        "label",
        "latitude",
        "longitude",
        "address",
        "source",
        "created_by",
        "created_at",
        "updated_at",
        "investigation_link",
    )

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("created_by").defer("notes")

    @admin.display(description="Case", ordering="person__case__name")
    def case_name(self, obj):
        return obj.person.case

    @admin.display(description="Investigation workflow")
    def investigation_link(self, obj):
        url = reverse("geo:poi_detail", args=(obj.person.case_id, obj.pk))
        return format_html('<a href="{}">Open point of interest</a>', url)


@admin.register(VehicleLocationImport)
class VehicleLocationImportAdmin(CaseScopedReadOnlyAdmin):
    list_display = (
        "document",
        "case",
        "status",
        "row_count",
        "invalid_count",
        "created_at",
    )
    list_filter = ("status", "created_at")
    search_fields = ("=id", "case__name", "document__title")
    fields = (
        "id",
        "case",
        "document",
        "selected_vehicle",
        "timezone",
        "parser_version",
        "uploaded_by",
        "requested_by",
        "status",
        "phase",
        "attempts",
        "error_code",
        "row_count",
        "processed_rows",
        "invalid_count",
        "duplicate_count",
        "duplicates_confirmed",
        "created_at",
        "started_at",
        "finished_at",
        "completed_at",
        "review_link",
    )

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .filter(
                document__case_id=F("case_id"),
                document__uploaded_by_id=F("uploaded_by_id"),
            )
            .filter(
                Q(selected_vehicle__isnull=True)
                | Q(selected_vehicle__case_id=F("case_id"))
            )
            .select_related(
                "document", "selected_vehicle", "uploaded_by", "requested_by"
            )
            .defer("groups", "vehicle_mapping")
        )

    @admin.display(description="Investigation workflow")
    def review_link(self, obj):
        url = reverse("geo:vehicle_import_review", args=(obj.case_id, obj.pk))
        return format_html('<a href="{}">Open paginated import review</a>', url)


def _case_trips_link(obj):
    url = reverse(
        "geo:case_map", args=(obj.case_id,), query={"vehicle_id": obj.vehicle_id}
    )
    return format_html('<a href="{}">Open case trips for this vehicle</a>', url)


@admin.register(VehicleTripState)
class VehicleTripStateAdmin(CaseScopedReadOnlyAdmin):
    list_display = ("vehicle", "case", "revision", "current_generation")
    search_fields = ("=id", "case__name", "vehicle__license_plate", "vehicle__vin")
    fields = ("id", "case", "vehicle", "revision", "current_generation", "trips_link")

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .filter(vehicle__case_id=F("case_id"))
            .filter(
                Q(current_generation__isnull=True)
                | Q(
                    current_generation__case_id=F("case_id"),
                    current_generation__vehicle_id=F("vehicle_id"),
                    current_generation__state_id=F("pk"),
                )
            )
            .select_related("vehicle", "current_generation")
            .defer(
                "current_generation__cursor",
                "current_generation__carry",
                "current_generation__metrics",
            )
        )

    @admin.display(description="Investigation workflow")
    def trips_link(self, obj):
        return _case_trips_link(obj)


@admin.register(VehicleTripGeneration)
class VehicleTripGenerationAdmin(CaseScopedReadOnlyAdmin):
    list_display = (
        "vehicle",
        "case",
        "status",
        "current_generation",
        "processed_sources",
        "total_sources",
        "created_at",
    )
    list_filter = ("status", "created_at")
    search_fields = ("=id", "case__name", "vehicle__license_plate", "vehicle__vin")
    fields = (
        "id",
        "case",
        "vehicle",
        "state",
        "revision",
        "current_generation",
        "previous_generation",
        "method_version",
        "requested_by",
        "status",
        "processed_sources",
        "total_sources",
        "attempts",
        "error_code",
        "created_at",
        "started_at",
        "finished_at",
        "geocode_status",
        "geocode_attempts",
        "geocode_error_code",
        "trips_link",
    )

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .filter(
                vehicle__case_id=F("case_id"),
                state__case_id=F("case_id"),
                state__vehicle_id=F("vehicle_id"),
            )
            .filter(
                Q(previous_generation__isnull=True)
                | Q(
                    previous_generation__case_id=F("case_id"),
                    previous_generation__vehicle_id=F("vehicle_id"),
                )
            )
            .select_related("vehicle", "state", "previous_generation", "requested_by")
            .defer(
                "cursor",
                "carry",
                "metrics",
                "previous_generation__cursor",
                "previous_generation__carry",
                "previous_generation__metrics",
            )
        )

    @admin.display(boolean=True, description="Current generation")
    def current_generation(self, obj):
        return obj.state.current_generation_id == obj.pk

    @admin.display(description="Investigation workflow")
    def trips_link(self, obj):
        return _case_trips_link(obj)


@admin.register(VehicleTrip)
class VehicleTripAdmin(CaseScopedReadOnlyAdmin):
    list_display = ("vehicle", "case", "start_at", "end_at", "kind", "source_count")
    list_filter = ("kind", "evidence_state", "start_at")
    search_fields = (
        "=id",
        "=generation__id",
        "case__name",
        "vehicle__license_plate",
        "vehicle__vin",
    )
    fields = (
        "id",
        "case",
        "vehicle",
        "generation",
        "evidence_state",
        "kind",
        "ordinal",
        "start_at",
        "end_at",
        "source_count",
        "largest_gap_seconds",
        "basis",
        "observations_link",
    )

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .filter(
                vehicle__case_id=F("case_id"),
                generation__case_id=F("case_id"),
                generation__vehicle_id=F("vehicle_id"),
                generation__state__case_id=F("case_id"),
                generation__state__vehicle_id=F("vehicle_id"),
                generation__status=VehicleTripGeneration.Status.COMPLETE,
            )
            .select_related("vehicle", "generation")
            .defer("generation__cursor", "generation__carry", "generation__metrics")
        )

    @admin.display(description="Investigation workflow")
    def observations_link(self, obj):
        if (
            obj.evidence_state == "active"
            and obj.kind == "trip"
            and saved_trips(obj.case, vehicle_id=obj.vehicle_id)
            .filter(pk=obj.pk)
            .exists()
        ):
            url = reverse("geo:trip_detail", args=(obj.case_id, obj.pk))
            return format_html('<a href="{}">Open trip observations</a>', url)
        return _case_trips_link(obj)
