"""Case-scoped operator access with confirmed superuser evidence deletion."""

from collections import Counter

from django.contrib import admin
from django.contrib.admin.utils import get_deleted_objects
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import F, Q
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html

from cases.admin_access import CaseScopedAdmin
from cases.permissions import accessible_cases

from .deletion import delete_evidence, deletion_block_reason, related_analysis_runs
from .forms import (
    CALL_KINDS,
    COMMON_FIELDS,
    FILE_KINDS,
    KINDS,
    ORIGINAL_FIELDS,
    TYPE_FIELDS,
    BaseEvidenceForm,
)
from .models import CallEvidenceAttachment
from .queries import same_case_evidence


class CallAttachmentInline(admin.StackedInline):
    model = CallEvidenceAttachment
    extra = 0
    can_delete = False
    fields = readonly_fields = (
        "transcript",
        "original_file",
        "uploaded_by",
        "created_at",
        "sha256",
        "audio_sha256",
        "inspect",
    )

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        if obj is None:
            parent_model = self.model._meta.get_field(self.fk_name).related_model
            return request.user.has_perm(
                f"evidence.view_{parent_model._meta.model_name}"
            ) or request.user.has_perm(
                f"evidence.change_{parent_model._meta.model_name}"
            )
        return (
            request.user.has_perm(f"evidence.view_{obj._meta.model_name}")
            or request.user.has_perm(f"evidence.change_{obj._meta.model_name}")
        ) and (
            obj.case.owner_id == request.user.pk
            or obj.case.memberships.filter(user=request.user).exists()
        )

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .filter(case__in=accessible_cases(request.user))
            .filter(
                Q(phone_call__case_id=F("case_id")) | Q(facetime__case_id=F("case_id"))
            )
        )

    @admin.display(description="Original audio")
    def original_file(self, obj):
        return obj.original_filename or "No audio supplied"

    @admin.display(description="Private evidence workflow")
    def inspect(self, obj):
        url = reverse(
            "evidence:detail",
            kwargs={
                "case_id": obj.case_id,
                "kind": "phone-call" if obj.phone_call_id else "facetime",
                "evidence_id": obj.phone_call_id or obj.facetime_id,
            },
        )
        return format_html(
            '<a href="{}#attachment-{}">Inspect or add retained material</a>',
            url,
            obj.pk,
        )


class PhoneCallAttachmentInline(CallAttachmentInline):
    fk_name = "phone_call"


class FaceTimeAttachmentInline(CallAttachmentInline):
    fk_name = "facetime"


@admin.register(*KINDS.values())
class EvidenceAdmin(CaseScopedAdmin):
    form = BaseEvidenceForm
    delete_confirmation_template = "admin/evidence/delete_confirmation.html"
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
        )
        return same_case_evidence(queryset)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name in ("person", "vehicle", "sender", "recipient"):
            kwargs["queryset"] = db_field.related_model.objects.filter(
                case__in=self.get_editable_cases(request)
            )
            kwargs["help_text"] = (
                "Choose a record belonging to the selected case. "
                "This reports an association, not verified identity or presence."
            )
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name in ("body", "address"):
            kwargs["strip"] = False
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    def get_inlines(self, request, obj=None):
        if obj is not None and self.kind in CALL_KINDS and obj.attachments.exists():
            return (
                (PhoneCallAttachmentInline,)
                if self.kind == "phone-call"
                else (FaceTimeAttachmentInline,)
            )
        return ()

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        if obj is not None:
            fields += ORIGINAL_FIELDS[self.kind]
            if self.kind in FILE_KINDS:
                fields += ("original_filename", "byte_size", "declared_content_type")
            if self.kind == "vehicle-location":
                fields += (
                    "import_batch",
                    "source_row",
                    "raw_row",
                    "speed_kmh",
                    "received_at",
                )
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
        if obj is not None and self.kind == "vehicle-location":
            fieldsets += (
                (
                    "Imported source values",
                    {
                        "fields": (
                            "import_batch",
                            "source_row",
                            "raw_row",
                            "speed_kmh",
                            "received_at",
                        )
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

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser and self.has_view_permission(request, obj)

    def get_deleted_objects(self, objs, request):
        related_runs = []
        for obj in objs:
            runs = list(related_analysis_runs(obj, self.kind))
            reason = deletion_block_reason(obj, runs)
            if reason:
                return [], {}, set(), [reason]
            related_runs.extend(runs)
        deleted, counts, permissions, protected = super().get_deleted_objects(
            objs, request
        )
        counts = Counter(counts)
        if related_runs:
            related, totals, missing, retained = get_deleted_objects(
                related_runs, request, self.admin_site
            )
            deleted.extend(related)
            counts.update(totals)
            permissions.update(missing)
            protected.extend(retained)
        return deleted, counts, permissions, protected

    def delete_model(self, request, obj):
        try:
            delete_evidence(request.user, obj.case_id, self.kind, obj.pk)
        except ValidationError as error:
            raise PermissionDenied from error

    def save_model(self, request, obj, form, change):
        if not change:
            obj.uploaded_by = request.user
        elif obj.is_archived != form.initial["is_archived"]:
            obj.archive_changed_at = timezone.now()
            obj.archive_changed_by = request.user
        super().save_model(request, obj, form, change)
