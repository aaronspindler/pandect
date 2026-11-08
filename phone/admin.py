from django.contrib import admin
from django.db.models import F, Q
from django.urls import reverse
from django.utils.html import format_html

from cases.permissions import accessible_cases
from core.evidence.admin import EvidenceAdmin
from phone.forms import (
    FaceTimeEvidenceForm,
    PhoneCallEvidenceForm,
    TextMessageEvidenceForm,
)
from phone.models import (
    CallEvidenceAttachment,
    FaceTimeEvidence,
    PhoneCallEvidence,
    TextMessageEvidence,
)


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
