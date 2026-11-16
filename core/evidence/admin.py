"""Case-scoped operator access with confirmed superuser evidence deletion."""

import json
from collections import Counter

from django.conf import settings
from django.contrib import admin
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.contrib.admin.widgets import AdminTextareaWidget
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import StreamingHttpResponse
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone

from cases.admin_access import CaseScopedAdmin
from cases.permissions import get_case
from core.evidence.deletion import (
    CHANGED,
    deletion_manifest_response,
    deletion_review_context,
    execute_deletion,
    get_deletion_target,
    plan_deletion,
    plan_deletions,
)
from core.evidence.forms import BaseEvidenceForm
from core.evidence.queries import same_case_evidence


class EvidenceAdmin(CaseScopedAdmin):
    actions = ("delete_selected",)
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

    def get_actions(self, request, action_location=admin.ActionLocation.CHANGE_LIST):
        if not settings.DEBUG or not self.has_delete_permission(request):
            return {}
        return super().get_actions(request, action_location=action_location)

    @admin.action(
        permissions=["delete"], description="Delete selected evidence (DEBUG only)"
    )
    @transaction.atomic
    def delete_selected(self, request, queryset):
        if not settings.DEBUG or not self.has_delete_permission(request):
            raise PermissionDenied
        confirming = request.POST.get("post") == "yes"
        # Lock cases in a stable order before rebuilding the complete inventory.
        if confirming:
            for case_id in sorted(set(queryset.values_list("case_id", flat=True))):
                get_case(request.user, case_id, lock=True)
            queryset = queryset.select_for_update(of=("self",))
        # Review/logging need identities and titles, not original bodies. Keep
        # file keys loaded for the post-delete cleanup signal.
        fields = ["id", "case", "title"]
        if self.file_evidence:
            fields.append("file")
        items = list(
            queryset.select_related(None).only(*fields).order_by("case_id", "pk")
        )
        groups = {}
        for item in items:
            groups.setdefault(item.case_id, []).append(item)
        plans = [plan_deletions(group, self.kind) for group in groups.values()]
        blockers = {plan.blocker for plan in plans if plan.blocker}
        revisions = request.POST.getlist("revision")
        changed = bool(revisions or confirming) and (
            len(revisions) != len(plans)
            or any(
                not plan.matches(revision, request.user)
                for plan, revision in zip(plans, revisions, strict=False)
            )
        )
        if (
            confirming
            and request.POST.get("confirm") == "yes"
            and not changed
            and not blockers
        ):
            self.log_deletions(request, items)
            for plan in plans:
                execute_deletion(plan, request.user)
            self.message_user(
                request, f"Permanently deleted {len(items)} evidence records."
            )
            return None
        # A blocked cascade must never disclose labels from inconsistent links.
        counts = (
            sum((plan.counts for plan in plans), Counter())
            if not blockers
            else Counter()
        )
        if request.POST.get("download") == "manifest" and not blockers and not changed:
            entries = [
                dict(entry, case_id=str(plan.item.case_id), case=str(plan.item.case))
                for plan in plans
                for entry in plan.entries
            ]
            response = StreamingHttpResponse(
                json.JSONEncoder().iterencode(
                    {
                        "schema": "pandect.evidence-bulk-deletion.v1",
                        "evidence_ids": [str(item.pk) for item in items],
                        "counts": dict(counts),
                        "entries": entries,
                    }
                ),
                content_type="application/octet-stream",
            )
            response["Content-Disposition"] = (
                'attachment; filename="evidence-deletion.json"'
            )
            response["Cache-Control"] = "private, no-store"
            response["X-Content-Type-Options"] = "nosniff"
            return response
        request.current_app = self.admin_site.name
        return TemplateResponse(
            request,
            "core/admin/bulk_delete_confirmation.html",
            {
                **self.admin_site.each_context(request),
                "title": "Delete selected evidence",
                "opts": self.opts,
                "media": self.media,
                "preserved_filters": self.get_preserved_filters(request),
                "items": items,
                "selected_items": items[:1]
                if request.POST.get("select_across") == "1"
                else items,
                "action_checkbox_name": ACTION_CHECKBOX_NAME,
                "revisions": [plan.revision(request.user) for plan in plans],
                "select_across": request.POST.get("select_across", "0"),
                "blockers": blockers,
                "deletion_plan": {"counts": counts},
                **(deletion_summary(plans) if not blockers else {}),
                "inventory_changed": CHANGED if changed else "",
                "deletion_download_post": True,
            },
            status=409 if changed else 200,
        )

    def get_deleted_objects(self, objs, request):
        # Individual deletion uses the same plan for review and mutation.
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
        if not plan.blocker:
            context.update(deletion_summary([plan]))
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


def deletion_summary(plans):
    """Summarize complete plans without copying their individual entries."""
    record_counts = Counter()
    selection_count = 0
    for plan in plans:
        for entry in plan.entries:
            if entry["action"] == "Delete record":
                record_counts[entry["type"]] += 1
            elif entry["action"] == "Retain unavailable references":
                selection_count += 1
    return {
        "deletion_record_counts": sorted(record_counts.items()),
        "deletion_selection_count": selection_count,
        "deletion_cases": sorted(
            (
                {
                    "name": str(plan.item.case),
                    "selected": len(plan.items),
                    "counts": plan.counts,
                }
                for plan in plans
            ),
            key=lambda case: case["name"].casefold(),
        ),
    }
