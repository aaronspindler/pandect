from django.contrib import admin
from django.db.models import F
from django.urls import reverse
from django.utils.html import format_html

from cases.admin_access import CaseScopedReadOnlyAdmin
from socialmedia.models import (
    InstagramCapture,
    InstagramCheck,
    InstagramComparison,
    InstagramMonitor,
    InstagramTargetApproval,
    SocialAccount,
)
from socialmedia.monitor_jobs import check_message


@admin.register(SocialAccount)
class SocialAccountAdmin(CaseScopedReadOnlyAdmin):
    case_lookup = "person__case"
    fields = (
        "person",
        "platform",
        "username",
        "source",
        "created_at",
        "updated_at",
        "person_accounts",
    )
    list_display = ("username", "platform", "person", "updated_at")
    search_fields = ("username", "platform", "person__first_name", "person__last_name")

    @admin.display(description="Case workspace")
    def person_accounts(self, obj):
        url = reverse(
            "people:detail",
            args=(obj.person.case_id, obj.person_id),
            fragment="social-accounts",
        )
        return format_html('<a href="{}">Open person’s social accounts</a>', url)


@admin.register(InstagramTargetApproval)
class InstagramTargetApprovalAdmin(CaseScopedReadOnlyAdmin):
    case_owner_only = True
    fields = ("case", "source", "target_id", "case_settings")
    list_display = ("target_id", "case", "source")
    list_filter = ("source",)
    search_fields = ("=target_id",)

    @admin.display(description="Case workspace")
    def case_settings(self, obj):
        url = reverse(
            "cases:case_settings", args=(obj.case_id,), fragment="instagram-monitoring"
        )
        return format_html('<a href="{}">Open Instagram monitoring settings</a>', url)


@admin.register(InstagramMonitor)
class InstagramMonitorAdmin(CaseScopedReadOnlyAdmin):
    fields = (
        "case",
        "target",
        "source",
        "status",
        "interval_hours",
        "next_check_at",
        "created_at",
        "case_monitor",
    )
    list_display = ("target", "case", "status", "source", "next_check_at")
    list_filter = ("status", "source")
    search_fields = ("target", "=id")
    ordering = ("-created_at", "-pk")

    @admin.display(description="Case workspace")
    def case_monitor(self, obj):
        url = reverse("instagram:detail", args=(obj.case_id, obj.pk))
        return format_html('<a href="{}">Open monitor and check history</a>', url)


@admin.register(InstagramCheck)
class InstagramCheckAdmin(CaseScopedReadOnlyAdmin):
    case_lookup = "monitor__case"
    fields = ("monitor", "status", "started_at", "finished_at", "check_outcome")
    list_display = ("monitor", "status", "started_at", "finished_at")
    list_filter = ("status",)
    search_fields = ("monitor__target", "=id")

    @admin.display(description="Check outcome")
    def check_outcome(self, obj):
        return check_message(obj.error) if obj.error else None


@admin.register(InstagramCapture)
class InstagramCaptureAdmin(CaseScopedReadOnlyAdmin):
    case_lookup = "monitor_check__monitor__case"
    fields = (
        "monitor_check",
        "direction",
        "entry_count",
        "complete",
        "reason",
        "collector_version",
        "case_capture",
    )
    list_display = ("__str__", "monitor", "monitor_check", "complete")
    list_filter = ("direction", "complete")
    search_fields = ("monitor_check__monitor__target", "=id", "=evidence__id")
    ordering = ("-monitor_check__started_at", "-pk")

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .filter(evidence__case_id=F("monitor_check__monitor__case_id"))
            .defer("entries")
        )

    @admin.display(description="Monitor", ordering="monitor_check__monitor__target")
    def monitor(self, obj):
        return obj.monitor_check.monitor

    @admin.display(description="Case workspace")
    def case_capture(self, obj):
        monitor = obj.monitor_check.monitor
        url = reverse("instagram:capture", args=(monitor.case_id, monitor.pk, obj.pk))
        return format_html('<a href="{}">Inspect retained capture</a>', url)


@admin.register(InstagramComparison)
class InstagramComparisonAdmin(CaseScopedReadOnlyAdmin):
    case_lookup = "capture__monitor_check__monitor__case"
    fields = ("capture", "previous", "addition_count", "case_comparison")
    list_display = ("capture", "captured_at", "addition_count")
    search_fields = ("capture__monitor_check__monitor__target", "=id")
    ordering = ("-capture__monitor_check__started_at", "-pk")

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .filter(
                capture__evidence__case_id=F(
                    "capture__monitor_check__monitor__case_id"
                ),
                previous__evidence__case_id=F("capture__evidence__case_id"),
                previous__monitor_check__monitor_id=F(
                    "capture__monitor_check__monitor_id"
                ),
            )
            .select_related("previous")
            .defer("additions", "capture__entries", "previous__entries")
        )

    @admin.display(
        description="Captured at", ordering="capture__monitor_check__started_at"
    )
    def captured_at(self, obj):
        return obj.capture.monitor_check.started_at

    @admin.display(description="Case workspace")
    def case_comparison(self, obj):
        monitor = obj.capture.monitor_check.monitor
        url = reverse(
            "instagram:capture",
            args=(monitor.case_id, monitor.pk, obj.capture_id),
            query={"show": "changes"},
        )
        return format_html('<a href="{}">Inspect observed additions</a>', url)
