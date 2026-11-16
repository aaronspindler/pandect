"""Case-scoped operator access with confirmed superuser evidence deletion."""

from collections import Counter

from django.contrib import admin
from django.contrib.admin.widgets import AdminTextareaWidget
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone

from cases.admin_access import CaseScopedAdmin
from core.evidence.deletion import (
    CHANGED,
    deletion_manifest_response,
    deletion_review_context,
    execute_deletion,
    get_deletion_target,
    plan_deletion,
)
from core.evidence.forms import BaseEvidenceForm
from core.evidence.queries import same_case_evidence


class EvidenceAdmin(CaseScopedAdmin):
    form = BaseEvidenceForm
    file_evidence = False
    import_fields = ()
    delete_confirmation_template = "core/admin/delete_confirmation.html"
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
        "description",
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
        css = {"all": ("core/admin.css",)}

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
        if db_field.name == "source_description":
            kwargs["widget"] = AdminTextareaWidget(attrs={"rows": 3})
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        if obj is not None:
            fields += self.original_fields
            if self.file_evidence:
                fields += ("original_filename", "byte_size", "declared_content_type")
            if self.import_fields:
                fields += self.import_fields
        return fields

    def get_fieldsets(self, request, obj=None):
        originals = self.original_fields
        if obj is not None and self.file_evidence:
            # A read-only FileField would render the storage backend's URL.
            originals = ("original_file", "byte_size", "declared_content_type")
        fieldsets = (
            (None, {"fields": ("case", "title", "source")}),
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
            (
                "Additional source details",
                {
                    "classes": ("collapse",),
                    "fields": ("source_url", "source_description", "collected_at"),
                },
            ),
        )
        if obj is not None and obj.description:
            fieldsets += (
                (
                    "Saved description",
                    {"classes": ("collapse",), "fields": ("description",)},
                ),
            )
        if obj is not None and self.import_fields:
            fieldsets += (
                (
                    "Imported source values",
                    {
                        "classes": ("collapse",),
                        "fields": self.import_fields,
                    },
                ),
            )
        if self.type_fields:
            fieldsets += (
                (
                    "Declared metadata",
                    {
                        "classes": ("collapse",),
                        "fields": self.type_fields,
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
                        "classes": ("collapse",),
                        "fields": (
                            "id",
                            "uploaded_by",
                            "created_at",
                            "edited_at",
                            "sha256",
                            "submission_id",
                        ),
                    },
                ),
            )
        return fieldsets

    @admin.display(description="Original file")
    def original_file(self, obj):
        return obj.original_filename

    def view_on_site(self, obj):
        return reverse(
            "core:evidence:detail",
            kwargs={"case_id": obj.case_id, "kind": self.kind, "evidence_id": obj.pk},
        )

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser and self.has_view_permission(request, obj)

    def get_deleted_objects(self, objs, request):
        # Bulk deletion is disabled; the same plan supplies review and mutation.
        obj = objs[0]
        if request.method == "POST":
            # Django holds the delete transaction through this hook and delete_model.
            _, obj = get_deletion_target(
                request.user, obj.case_id, self.kind, obj.pk, lock=True
            )
        plan = plan_deletion(obj, self.kind)
        request.evidence_deletion_plan = plan
        if plan.blocker:
            return [], {}, set(), [plan.blocker]
        if request.method == "POST" and not plan.matches(
            request.POST.get("revision", ""), request.user
        ):
            return [], {}, set(), [CHANGED]
        counts = Counter(
            row["type"] for row in plan.entries if row["action"] == "Delete record"
        )
        return [], counts, set(), []

    def render_delete_form(self, request, context):
        plan = request.evidence_deletion_plan
        context.update(deletion_review_context(request, plan))
        changed = (
            context["protected"] == [CHANGED] or context["inventory_changed"]
        ) and not plan.blocker
        if changed:
            context.update(
                protected=[],
                deletion_changed=CHANGED if request.method == "POST" else "",
                title="Review deletion",
            )
        response = super().render_delete_form(request, context)
        if changed:
            response.status_code = 409
        return response

    def delete_view(self, request, object_id, extra_context=None):
        if request.method == "GET" and request.GET.get("download") == "manifest":
            obj = self.get_object(request, object_id)
            if obj is None or not self.has_delete_permission(request, obj):
                raise PermissionDenied
            plan = plan_deletion(obj, self.kind)
            if plan.blocker:
                raise PermissionDenied
            if not request.GET.get("review_revision") or plan.matches(
                request.GET["review_revision"], request.user
            ):
                return deletion_manifest_response(plan)
        return super().delete_view(request, object_id, extra_context)

    def delete_model(self, request, obj):
        execute_deletion(request.evidence_deletion_plan, request.user)

    def save_model(self, request, obj, form, change):
        if not change:
            obj.uploaded_by = request.user
        elif obj.is_archived != form.initial["is_archived"]:
            obj.archive_changed_at = timezone.now()
            obj.archive_changed_by = request.user
        super().save_model(request, obj, form, change)
