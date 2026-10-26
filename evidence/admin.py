"""Case-scoped operator access without replacing or deleting originals."""

from django import forms
from django.conf import settings
from django.contrib import admin
from django.db.models import F, Q
from django.urls import reverse
from django.utils import timezone

from cases.models import Case, CaseMembership

from .forms import COMMON_FIELDS, FILE_KINDS, KINDS, ORIGINAL_FIELDS, TYPE_FIELDS


class EvidenceAdminForm(forms.ModelForm):
    def clean_file(self):
        original = self.cleaned_data["file"]
        if original.size > settings.PANDECT_MAX_FILE_BYTES:
            raise forms.ValidationError(
                "This file exceeds the configured upload limit."
            )
        if len(getattr(original, "content_type", "") or "") > 255:
            raise forms.ValidationError("The declared content type is too long.")
        return original

    def clean_body(self):
        body = self.cleaned_data["body"]
        if len(body.encode("utf-8")) > settings.PANDECT_MAX_TEXT_BYTES:
            raise forms.ValidationError(
                "This text exceeds the configured capture limit."
            )
        return body

    def clean(self):
        cleaned = super().clean()
        for name in ("description", "source_description"):
            if len(cleaned.get(name, "")) > 20000:
                self.add_error(name, "Keep this description within 20,000 characters.")
        return cleaned


@admin.register(*KINDS.values())
class EvidenceAdmin(admin.ModelAdmin):
    form = EvidenceAdminForm
    list_display = ("title", "case", "uploaded_by", "is_archived", "created_at")
    list_select_related = ("case", "uploaded_by")
    list_filter = ("is_archived",)
    search_fields = (
        "title",
        "description",
        "source",
        "source_url",
        "source_description",
    )
    readonly_fields = (
        "id",
        "uploaded_by",
        "created_at",
        "edited_at",
        "sha256",
        "submission_id",
        "archive_changed_at",
        "archive_changed_by",
        "original_file",
    )
    actions = None

    class Media:
        css = {"all": ("evidence/admin.css",)}

    def __init__(self, model, admin_site):
        super().__init__(model, admin_site)
        self.kind = next(
            kind for kind, candidate in KINDS.items() if candidate is model
        )

    def get_queryset(self, request):
        queryset = (
            super()
            .get_queryset(request)
            .select_related("case", "uploaded_by", "archive_changed_by")
            .filter(
                Q(case__owner=request.user) | Q(case__memberships__user=request.user)
            )
            .distinct()
        )
        if self.kind == "person-location":
            queryset = queryset.filter(person__case_id=F("case_id"))
        return queryset

    def get_editable_cases(self, request):
        return Case.objects.filter(
            Q(owner=request.user)
            | Q(
                memberships__user=request.user,
                memberships__role=CaseMembership.Role.EDITOR,
            )
        ).distinct()

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "case":
            kwargs["queryset"] = self.get_editable_cases(request)
        elif db_field.name == "person":
            kwargs["queryset"] = db_field.related_model.objects.filter(
                case__in=self.get_editable_cases(request)
            )
            kwargs["help_text"] = (
                "Choose a person belonging to the selected case. "
                "This reports an association, not verified identity or presence."
            )
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name in ("body", "address"):
            kwargs["strip"] = False
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        if obj is not None:
            fields += ("case", *ORIGINAL_FIELDS[self.kind])
            if self.kind in FILE_KINDS:
                fields += ("original_filename", "byte_size", "declared_content_type")
        return fields

    def get_fieldsets(self, request, obj=None):
        originals = ORIGINAL_FIELDS[self.kind]
        if obj is not None and self.kind in FILE_KINDS:
            # A read-only FileField would render the storage backend's URL.
            originals = ("original_file", "byte_size", "declared_content_type")
        fieldsets = (
            (None, {"fields": ("case", *COMMON_FIELDS)}),
            (
                "Original evidence",
                {
                    "fields": originals,
                    "description": (
                        "Originals are retained and cannot be replaced. "
                        "Use View on site to open the case evidence workflow."
                    ),
                },
            ),
        )
        if TYPE_FIELDS[self.kind]:
            fieldsets += (
                (
                    "Declared metadata",
                    {
                        "fields": TYPE_FIELDS[self.kind],
                        "description": (
                            "Investigator-supplied information; "
                            "not automatically verified."
                        ),
                    },
                ),
            )
        if obj is not None:
            fieldsets += (
                (
                    "Retention",
                    {
                        "fields": (
                            "is_archived",
                            "archive_changed_at",
                            "archive_changed_by",
                        ),
                        "description": (
                            "Archiving retains the original. Uncheck to restore it."
                        ),
                    },
                ),
                (
                    "Integrity and attribution",
                    {
                        "fields": (
                            "id",
                            "uploaded_by",
                            "created_at",
                            "edited_at",
                            "sha256",
                            "submission_id",
                        )
                    },
                ),
            )
        return fieldsets

    @admin.display(description="Original file")
    def original_file(self, obj):
        return obj.original_filename

    def view_on_site(self, obj):
        return reverse(
            "evidence:detail",
            kwargs={"case_id": obj.case_id, "kind": self.kind, "evidence_id": obj.pk},
        )

    def has_view_permission(self, request, obj=None):
        return super().has_view_permission(request, obj) and (
            obj is None
            or obj.case.owner_id == request.user.pk
            or obj.case.memberships.filter(user=request.user).exists()
        )

    def has_add_permission(self, request):
        return (
            super().has_add_permission(request)
            and self.get_editable_cases(request).exists()
        )

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and (
            obj is None
            or obj.case.owner_id == request.user.pk
            or obj.case.memberships.filter(
                user=request.user, role=CaseMembership.Role.EDITOR
            ).exists()
        )

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        if not change:
            obj.uploaded_by = request.user
        elif obj.is_archived != form.initial["is_archived"]:
            obj.archive_changed_at = timezone.now()
            obj.archive_changed_by = request.user
        super().save_model(request, obj, form, change)
