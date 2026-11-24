from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.utils.translation import gettext_lazy as _

from cases.admin_access import AdminCascadeMixin

from .models import User


@admin.register(User)
class PandectUserAdmin(AdminCascadeMixin, UserAdmin):
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        (_("Personal info"), {"fields": ("first_name", "last_name", "email")}),
        (
            _("Access"),
            {"fields": ("is_active", "is_staff", "is_superuser")},
        ),
        (
            _("Advanced permissions"),
            {"fields": ("groups", "user_permissions"), "classes": ("collapse",)},
        ),
        (
            _("Important dates"),
            {"fields": ("last_login", "date_joined"), "classes": ("collapse",)},
        ),
    )
