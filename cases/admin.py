from django.contrib import admin
from django.forms.models import BaseInlineFormSet
from django.urls import reverse

from core.evidence.types import KINDS

from .admin_access import CaseAdminCascadeMixin, CaseScopedReadOnlyAdmin
from .models import Case, CaseActivity, CaseInvitation, CaseMembership
from .permissions import accessible_cases


class CaseMembershipFormSet(BaseInlineFormSet):
    def add_fields(self, form, index):
        super().add_fields(form, index)
        # Django scopes inline instances to the parent; scope submitted IDs too.
        form.fields[self.model._meta.pk.name].queryset = self.get_queryset()


class CaseMembershipInline(admin.StackedInline):
    model = CaseMembership
    formset = CaseMembershipFormSet
    fields = ("user", "role")
    extra = 1

    def has_view_permission(self, request, obj=None):
        return super().has_view_permission(request, obj) and (
            obj is None or obj.owner_id == request.user.pk
        )

    def has_add_permission(self, request, obj=None):
        return super().has_add_permission(request, obj) and (
            obj is None or obj.owner_id == request.user.pk
        )

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and (
            obj is None or obj.owner_id == request.user.pk
        )

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and (
            obj is None or obj.owner_id == request.user.pk
        )


@admin.register(Case)
class CaseAdmin(CaseAdminCascadeMixin, admin.ModelAdmin):
    list_display = ("__str__", "owner", "created_at")
    list_select_related = ("owner",)
    search_fields = ("name", "=id")
    ordering = ("-created_at", "-pk")
    readonly_fields = ("owner", "created_at")
    inlines = (CaseMembershipInline,)
    actions = None

    def lookup_allowed(self, lookup, value, request):
        return False

    def get_inlines(self, request, obj=None):
        if obj is not None and obj.owner_id == request.user.pk:
            return super().get_inlines(request, obj)
        return ()

    def get_queryset(self, request):
        return (
            super().get_queryset(request).filter(pk__in=accessible_cases(request.user))
        )

    def has_view_permission(self, request, obj=None):
        return super().has_view_permission(request, obj) and (
            obj is None
            or obj.owner_id == request.user.pk
            or obj.memberships.filter(user=request.user).exists()
        )

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and (
            obj is None or obj.owner_id == request.user.pk
        )

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and (
            obj is None or obj.owner_id == request.user.pk
        )

    def get_fieldsets(self, request, obj=None):
        if obj is None:
            return (
                (
                    None,
                    {
                        "fields": (),
                        "description": (
                            "Save to create a case owned by you. "
                            "You can add collaborators after saving."
                        ),
                    },
                ),
            )
        return ((None, {"fields": self.readonly_fields}),)

    def save_model(self, request, obj, form, change):
        if change:
            # Existing case details are read-only. Only the inlines are saved,
            # so a stale admin instance cannot overwrite a newer case approval.
            return
        obj.owner = request.user
        super().save_model(request, obj, form, change)

    def get_deleted_objects(self, objs, request):
        deleted, counts, permissions, protected = super().get_deleted_objects(
            objs, request
        )
        # Evidence denies individual deletion. An owner may still confirm the
        # entire case cascade, with the ordinary per-model delete permissions.
        for model in KINDS.values():
            opts = model._meta
            if request.user.has_perm(f"{opts.app_label}.delete_{opts.model_name}"):
                permissions.discard(opts.verbose_name)
        return deleted, counts, permissions, protected


@admin.register(CaseActivity)
class CaseActivityAdmin(CaseScopedReadOnlyAdmin):
    list_display = ("case", "work", "evidence", "findings", "important")
    search_fields = ("=id", "=case__id", "case__name")
    fields = (
        "id",
        "case",
        "work",
        "evidence",
        "images",
        "findings",
        "people",
        "vehicles",
        "important",
    )


@admin.register(CaseInvitation)
class CaseInvitationAdmin(CaseScopedReadOnlyAdmin):
    case_owner_only = True
    list_display = ("email", "case", "role", "status", "created_at", "expires_at")
    list_filter = ("role", "delivery_failed")
    search_fields = ("email", "=id", "=case__id", "case__name")
    fields = (
        "id",
        "case",
        "email",
        "role",
        "status",
        "invited_by",
        "created_at",
        "expires_at",
        "last_sent_at",
        "delivery_failed",
        "accepted_at",
        "accepted_by",
        "revoked_at",
    )

    def view_on_site(self, obj):
        return reverse("cases:case_settings", args=[obj.case_id])
