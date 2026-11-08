from django.contrib import admin

from core.evidence.admin import EvidenceAdmin
from geo.forms import (
    LocationEvidenceForm,
    PersonLocationEvidenceForm,
    VehicleLocationEvidenceForm,
)
from geo.models import LocationEvidence, PersonLocationEvidence, VehicleLocationEvidence


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
