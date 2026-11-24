from django.contrib import admin
from django.db.models import F, Q
from django.urls import reverse
from django.utils.html import format_html

from cases.admin_access import CaseScopedReadOnlyAdmin
from cases.permissions import accessible_cases
from core.evidence.admin import EvidenceAdmin
from phone.conversation_instances import instance_url
from phone.conversations import pair_messages
from phone.forms import (
    FaceTimeEvidenceForm,
    PhoneCallEvidenceForm,
    TextMessageEvidenceForm,
)
from phone.models import (
    CallEvidenceAttachment,
    CommunicationContext,
    ConversationGrouping,
    ConversationGroupingRequest,
    ConversationSession,
    FaceTimeEvidence,
    PhoneCallEvidence,
    TextMessageEvidence,
)
from phone.saved_sessions import valid_sessions


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
                f"{parent_model._meta.app_label}.view_{parent_model._meta.model_name}"
            ) or request.user.has_perm(
                f"{parent_model._meta.app_label}.change_{parent_model._meta.model_name}"
            )
        return (
            request.user.has_perm(f"{obj._meta.app_label}.view_{obj._meta.model_name}")
            or request.user.has_perm(
                f"{obj._meta.app_label}.change_{obj._meta.model_name}"
            )
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
            "core:evidence:detail",
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


@admin.register(TextMessageEvidence)
class TextMessageEvidenceAdmin(EvidenceAdmin):
    kind = "text-message"
    original_fields = TextMessageEvidenceForm.original_fields
    type_fields = TextMessageEvidenceForm.type_fields


@admin.register(PhoneCallEvidence)
class PhoneCallEvidenceAdmin(EvidenceAdmin):
    kind = "phone-call"
    original_fields = PhoneCallEvidenceForm.original_fields
    type_fields = PhoneCallEvidenceForm.type_fields

    def get_inlines(self, request, obj=None):
        return (
            (PhoneCallAttachmentInline,)
            if obj is not None and obj.attachments.exists()
            else ()
        )


@admin.register(FaceTimeEvidence)
class FaceTimeEvidenceAdmin(EvidenceAdmin):
    kind = "facetime"
    original_fields = FaceTimeEvidenceForm.original_fields
    type_fields = FaceTimeEvidenceForm.type_fields

    def get_inlines(self, request, obj=None):
        return (
            (FaceTimeAttachmentInline,)
            if obj is not None and obj.attachments.exists()
            else ()
        )


@admin.register(CommunicationContext)
class CommunicationContextAdmin(CaseScopedReadOnlyAdmin):
    list_display = ("message", "case", "source_document", "source_row", "created_at")
    list_filter = ("created_at",)
    search_fields = ("=id", "=message__id", "source_document__title", "case__name")
    fields = (
        "id",
        "case",
        "message",
        "source_document",
        "import_run",
        "source_row",
        "source_start",
        "source_end",
        "source_sha256",
        "parser_version",
        "timezone",
        "created_at",
        "message_link",
    )

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .filter(
                message__case_id=F("case_id"),
                message__sender__case_id=F("case_id"),
                message__recipient__case_id=F("case_id"),
                source_document__case_id=F("case_id"),
                source_document__sha256=F("source_sha256"),
            )
            .filter(Q(import_run__isnull=True) | Q(import_run__case_id=F("case_id")))
            .select_related("message", "source_document", "import_run")
            .defer(
                "participants",
                "account_identifiers",
                "attachments",
                "source_context",
                "message__body",
                "import_run__inputs",
                "import_run__parameters",
            )
        )

    @admin.display(description="Investigation workflow")
    def message_link(self, obj):
        url = reverse(
            "core:evidence:detail",
            args=(obj.case_id, "text-message", obj.message_id),
        )
        return format_html('<a href="{}">Open retained message</a>', url)


def _same_case_groupings(queryset):
    return queryset.filter(
        batch__case_id=F("case_id"),
        first_person__case_id=F("case_id"),
        second_person__case_id=F("case_id"),
    )


@admin.register(ConversationGrouping)
class ConversationGroupingAdmin(CaseScopedReadOnlyAdmin):
    list_display = (
        "first_person",
        "second_person",
        "case",
        "source_count",
        "processed_count",
        "published_at",
    )
    list_filter = ("published_at",)
    search_fields = (
        "=id",
        "=batch__id",
        "case__name",
        "first_person__first_name",
        "first_person__last_name",
        "second_person__first_name",
        "second_person__last_name",
    )
    fields = (
        "id",
        "case",
        "batch",
        "first_person",
        "second_person",
        "method_version",
        "source_count",
        "processed_count",
        "published_at",
        "published_by",
        "sessions_link",
    )

    def get_queryset(self, request):
        return (
            _same_case_groupings(super().get_queryset(request))
            .select_related("batch", "first_person", "second_person", "published_by")
            .defer("parameters", "cursor", "batch__scope", "batch__preparation_errors")
        )

    @admin.display(description="Investigation workflow")
    def sessions_link(self, obj):
        url = reverse(
            "phone:conversation_detail",
            args=(obj.case_id, obj.first_person_id, obj.second_person_id),
            query={"tab": "instances"},
        )
        return format_html('<a href="{}">Open sessions and summaries</a>', url)


@admin.register(ConversationGroupingRequest)
class ConversationGroupingRequestAdmin(CaseScopedReadOnlyAdmin):
    list_display = ("grouping", "case", "batch", "error_code")
    search_fields = ("=id", "=grouping__id", "=batch__id", "case__name", "error_code")
    fields = ("id", "case", "grouping", "batch", "error_code")

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .filter(
                batch__case_id=F("case_id"),
                grouping__case_id=F("case_id"),
                grouping__in=_same_case_groupings(ConversationGrouping.objects.all()),
            )
            .select_related("grouping", "batch")
            .defer(
                "grouping__parameters",
                "grouping__cursor",
                "batch__scope",
                "batch__preparation_errors",
            )
        )


@admin.register(ConversationSession)
class ConversationSessionAdmin(CaseScopedReadOnlyAdmin):
    list_display = ("grouping", "case", "first_at", "last_at", "message_count")
    list_filter = ("first_at",)
    search_fields = ("=id", "=grouping__id", "case__name")
    fields = (
        "id",
        "case",
        "grouping",
        "first_at",
        "last_at",
        "message_count",
        "boundary_reason",
        "session_link",
    )
    ordering = ("-first_at", "pk")

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .filter(
                grouping__case_id=F("case_id"),
                grouping__published_at__isnull=False,
                grouping__in=_same_case_groupings(ConversationGrouping.objects.all()),
            )
            .select_related("grouping")
            .defer("grouping__parameters", "grouping__cursor")
        )

    @admin.display(description="Investigation workflow")
    def session_link(self, obj):
        grouping = obj.grouping
        pair = (grouping.first_person_id, grouping.second_person_id)
        messages = pair_messages(obj.case, *pair)
        if valid_sessions(obj.case, messages).filter(pk=obj.pk).exists():
            url = instance_url(obj.case, pair, obj.pk)
            return format_html('<a href="{}">Open saved session and messages</a>', url)
        url = reverse(
            "phone:conversation_detail",
            args=(obj.case_id, *pair),
            query={"tab": "instances"},
        )
        return format_html('<a href="{}">Open available sessions</a>', url)
