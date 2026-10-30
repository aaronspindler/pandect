from django.contrib import admin

from cases.admin_access import CaseScopedAdmin

from .models import Vehicle


@admin.register(Vehicle)
class VehicleAdmin(CaseScopedAdmin):
    list_display = ("__str__", "case", "license_plate", "vin", "updated_at")
    list_select_related = ("case",)
    search_fields = ("make", "model", "license_plate", "vin")
    readonly_fields = ("created_at", "updated_at")
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "case",
                    "make",
                    "model",
                    "year",
                    "color",
                    "license_plate",
                    "vin",
                    "notes",
                ),
                "description": (
                    "Enter at least a make, model, license plate, or VIN. "
                    "Record uncertainty and source context in notes."
                ),
            },
        ),
        ("Record dates", {"fields": ("created_at", "updated_at")}),
    )
