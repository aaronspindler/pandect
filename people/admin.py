from django.contrib import admin

from cases.admin_access import CaseScopedAdmin

from .models import Person


@admin.register(Person)
class PersonAdmin(CaseScopedAdmin):
    list_display = ("__str__", "case", "email", "phone_number", "updated_at")
    list_select_related = ("case",)
    search_fields = ("first_name", "last_name", "email", "phone_number")
    readonly_fields = ("created_at", "updated_at", "notes")
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "case",
                    "first_name",
                    "last_name",
                    "phone_number",
                    "email",
                    "address",
                ),
                "description": (
                    "Enter at least a first name, last name, phone number, or email."
                ),
            },
        ),
        (
            "Record dates",
            {"fields": ("created_at", "updated_at"), "classes": ("collapse",)},
        ),
    )

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        if obj is not None and obj.notes:
            return (
                *fieldsets,
                ("Saved notes", {"fields": ("notes",), "classes": ("collapse",)}),
            )
        return fieldsets
