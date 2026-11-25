from django.contrib import admin
from django.contrib.admin.utils import NestedObjects
from django.contrib.auth.admin import UserAdmin
from django.db import router
from django.utils.translation import gettext_lazy as _

from cases.admin_access import AdminCascadeMixin
from cases.deletion import inaccessible_case_records
from cases.permissions import accessible_cases

from .models import User


@admin.register(User)
class PandectUserAdmin(AdminCascadeMixin, UserAdmin):
    def get_deleted_objects(self, objs, request):
        collector = NestedObjects(using=router.db_for_write(self.model), origin=objs)
        collector.collect(objs)
        if inaccessible_case_records(
            collector.protected, accessible_cases(request.user), collector.using
        ):
            return (
                [],
                {},
                set(),
                [_("This account is required by retained case records.")],
            )
        _objects, _counts, perms_needed, protected = super().get_deleted_objects(
            objs, request
        )
        accounts = [str(obj) for obj in objs]
        return (
            accounts,
            {self.opts.verbose_name_plural: len(accounts)},
            perms_needed,
            protected,
        )

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
