"""Shared admin permissions for records belonging to one case."""

from django.contrib import admin
from django.db.models import Q

from .models import Case, CaseMembership
from .permissions import accessible_cases


class CaseScopedAdmin(admin.ModelAdmin):
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
