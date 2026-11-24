"""Shared admin permissions for records belonging to one case."""

from django.contrib import admin
from django.db.models import Q

from .models import Case, CaseMembership
from .permissions import accessible_cases


class AdminCascadeMixin:
    def get_deleted_objects(self, objs, request):
        # Preserve existing parent cascades when a dependent record gains a
        # read-only admin. The flag only lives during Django's collection step.
        request._pandect_admin_collecting_cascade = True
        try:
            return super().get_deleted_objects(objs, request)
        finally:
            del request._pandect_admin_collecting_cascade


class CaseScopedAdmin(AdminCascadeMixin, admin.ModelAdmin):
    actions = None

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("case")
            .filter(case__in=accessible_cases(request.user))
        )

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
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        return (*fields, "case") if obj is not None else fields

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
        return super().has_delete_permission(request, obj) and (
            obj is None or obj.case.owner_id == request.user.pk
        )


class CaseScopedReadOnlyAdmin(admin.ModelAdmin):
    """Inspect explicit fields without exposing workflow-managed mutations."""

    case_lookup = "case"
    case_owner_only = False
    actions = None
    fields = ("id",)
    list_display = ("id",)
    search_fields = ("=id",)
    show_full_result_count = False
    view_on_site = False

    def get_queryset(self, request):
        cases = accessible_cases(request.user)
        if self.case_owner_only:
            cases = cases.filter(owner=request.user)
        return (
            super()
            .get_queryset(request)
            .select_related(self.case_lookup)
            .filter(**{f"{self.case_lookup}__in": cases})
        )

    def get_readonly_fields(self, request, obj=None):
        return self.fields

    def lookup_allowed(self, lookup, value, request):
        # Django also accepts arbitrary local field lookups by default. Hidden
        # payloads and credentials must not be queryable through the list URL.
        field, _, operator = lookup.partition("__")
        return field in self.get_list_filter(request) and operator in (
            "",
            "exact",
            "isnull",
            "gte",
            "lt",
        )

    def has_view_permission(self, request, obj=None):
        return super().has_view_permission(request, obj) and (
            obj is None or self.get_queryset(request).filter(pk=obj.pk).exists()
        )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return obj is not None and getattr(
            request, "_pandect_admin_collecting_cascade", False
        )
