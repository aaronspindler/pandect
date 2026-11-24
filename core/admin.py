from django.contrib import admin
from django.db.models import F, Q
from django.urls import reverse
from django_tasks_db.models import DBTaskResult

from cases.admin_access import CaseScopedReadOnlyAdmin

from .models import (
    AnalysisArtifact,
    AnalysisFinding,
    AnalysisFindingSource,
    AnalysisResult,
    AnalysisReview,
    AnalysisRun,
    CaseAnalysisBatch,
    CaseSearchIndex,
    ChatMatch,
    ChatSource,
    ChatTurn,
    EvidenceChat,
    ExtractedPassage,
    GatewayRequest,
    ImportantItem,
    ImportantNote,
    SavedSelection,
    SearchDocument,
    SearchParent,
    SearchParentScan,
    SearchPassage,
)
from .models.search import SOURCE_FIELDS
from .models.search_parents import PARENT_ORIGINS

# The queue has no case boundary. django_tasks_db registers it before this app.
# Private deletion reviews and storage cleanup work use their existing workflows.
admin.site.unregister(DBTaskResult)


class CoreRecordAdmin(CaseScopedReadOnlyAdmin):
    """Read only the diagnostic columns, with relations checked against the case."""

    search_fields = ("=id", "case__name")
    same_case_relations = ()

    def get_queryset(self, request):
        queryset = super().get_queryset(request).only(*self.fields)
        case_id = F(f"{self.case_lookup}__pk")
        for relation in self.same_case_relations:
            queryset = queryset.filter(
                Q(**{f"{relation}__isnull": True})
                | Q(**{f"{relation}__case_id": case_id})
            )
        return queryset


@admin.register(CaseAnalysisBatch)
class CaseAnalysisBatchAdmin(CoreRecordAdmin):
    list_display = ("id", "case", "status", "queued_runs", "created_at")
    list_filter = ("status", "created_at")
    fields = (
        "id",
        "case",
        "requested_by",
        "status",
        "source_kind",
        "processed_sources",
        "unavailable_sources",
        "queued_runs",
        "error_code",
        "created_at",
    )

    def view_on_site(self, obj):
        return reverse(
            "core:analysis:processing", args=(obj.case_id,), query={"batch": obj.pk}
        )


@admin.register(AnalysisRun)
class AnalysisRunAdmin(CoreRecordAdmin):
    list_display = ("operation", "case", "status", "progress", "created_at")
    list_filter = ("status", "external_processing", "created_at")
    search_fields = (*CoreRecordAdmin.search_fields, "operation", "=error_code")
    same_case_relations = ("batch", "previous_run")
    fields = (
        "id",
        "case",
        "operation",
        "method_version",
        "requested_by",
        "batch",
        "previous_run",
        "external_processing",
        "status",
        "stage",
        "progress",
        "attempts",
        "error_code",
        "created_at",
        "started_at",
        "finished_at",
    )

    def view_on_site(self, obj):
        return reverse("core:analysis:detail", args=(obj.case_id, obj.pk))


@admin.register(AnalysisResult)
class AnalysisResultAdmin(CoreRecordAdmin):
    list_display = ("id", "case", "run_id", "created_at")
    list_filter = ("created_at",)
    ordering = ("-created_at", "id")
    search_fields = (*CoreRecordAdmin.search_fields, "=run__id", "run__operation")
    same_case_relations = ("run",)
    fields = ("id", "case", "run", "created_at")

    def view_on_site(self, obj):
        return reverse("core:analysis:detail", args=(obj.case_id, obj.run_id))


@admin.register(AnalysisFinding)
class AnalysisFindingAdmin(CoreRecordAdmin):
    list_display = ("id", "case", "item_index", "review_signal", "weak")
    list_filter = ("weak", "hidden_from_catalogue")
    search_fields = (*CoreRecordAdmin.search_fields, "=result__id", "=review_signal")
    same_case_relations = ("result", "result__run")
    fields = (
        "id",
        "case",
        "result",
        "item_index",
        "weak",
        "review_signal",
        "hidden_from_catalogue",
    )

    def view_on_site(self, obj):
        from core.analysis.important import retained_finding_url

        return retained_finding_url(obj.case_id, obj.result.run, obj.item_index)


@admin.register(AnalysisFindingSource)
class AnalysisFindingSourceAdmin(CoreRecordAdmin):
    list_display = ("id", "case", "kind")
    search_fields = (*CoreRecordAdmin.search_fields, "=finding__id", "=kind")
    same_case_relations = ("finding", "finding__result", "finding__result__run")
    fields = ("id", "case", "finding", "kind", "source_sha256")


@admin.register(AnalysisReview)
class AnalysisReviewAdmin(CoreRecordAdmin):
    list_display = ("id", "case", "decision", "item_index", "created_at")
    list_filter = ("decision", "created_at")
    search_fields = (*CoreRecordAdmin.search_fields, "=result__id")
    same_case_relations = ("result", "result__run")
    fields = (
        "id",
        "case",
        "result",
        "decision",
        "item_index",
        "reviewed_by",
        "created_at",
    )


@admin.register(ExtractedPassage)
class ExtractedPassageAdmin(CoreRecordAdmin):
    list_display = ("id", "case", "kind", "item_index")
    search_fields = (*CoreRecordAdmin.search_fields, "=result__id", "=kind")
    same_case_relations = ("result", "result__run")
    fields = ("id", "case", "result", "kind", "item_index", "evidence_sha256")


@admin.register(AnalysisArtifact)
class AnalysisArtifactAdmin(CoreRecordAdmin):
    list_display = ("id", "case", "kind", "width", "height")
    list_filter = ("kind",)
    search_fields = (*CoreRecordAdmin.search_fields, "=result__id")
    same_case_relations = ("result", "result__run")
    fields = ("id", "case", "result", "kind", "sha256", "width", "height")


@admin.register(GatewayRequest)
class GatewayRequestAdmin(CoreRecordAdmin):
    list_display = ("id", "case", "operation", "status", "created_at")
    list_filter = ("operation", "created_at")
    ordering = ("-created_at", "id")
    search_fields = (
        *CoreRecordAdmin.search_fields,
        "=run__id",
        "=chat_turn__id",
        "=status",
    )
    same_case_relations = ("run", "chat_turn", "chat_turn__chat")
    fields = (
        "id",
        "case",
        "run",
        "chat_turn",
        "operation",
        "model",
        "status",
        "request_bytes",
        "response_bytes",
        "input_tokens",
        "output_tokens",
        "created_at",
    )


@admin.register(EvidenceChat)
class EvidenceChatAdmin(CoreRecordAdmin):
    list_display = ("title", "case", "created_at")
    list_filter = ("created_at",)
    ordering = ("-created_at", "id")
    search_fields = (*CoreRecordAdmin.search_fields, "title")
    fields = ("id", "case", "title", "created_by", "created_at")

    def view_on_site(self, obj):
        return reverse("core:chat:detail", args=(obj.case_id, obj.pk))


@admin.register(ChatTurn)
class ChatTurnAdmin(CoreRecordAdmin):
    list_display = ("id", "case", "ordinal", "status", "created_at")
    list_filter = ("status", "created_at")
    ordering = ("-created_at", "id")
    search_fields = (*CoreRecordAdmin.search_fields, "=chat__id", "=error_code")
    same_case_relations = ("chat",)
    fields = (
        "id",
        "case",
        "chat",
        "ordinal",
        "asked_by",
        "status",
        "mode",
        "total",
        "reviewed",
        "gaps",
        "error_code",
        "created_at",
        "started_at",
        "completed_at",
    )

    def view_on_site(self, obj):
        return reverse("core:chat:reply", args=(obj.case_id, obj.chat_id, obj.pk))


@admin.register(ChatSource)
class ChatSourceAdmin(CoreRecordAdmin):
    list_display = ("id", "case", "evidence_kind", "status")
    search_fields = (*CoreRecordAdmin.search_fields, "=turn__id", "=status")
    same_case_relations = ("turn", "turn__chat")
    fields = ("id", "case", "turn", "evidence_kind", "status", "cursor")


@admin.register(ChatMatch)
class ChatMatchAdmin(CoreRecordAdmin):
    list_display = ("id", "case", "start", "end")
    search_fields = (*CoreRecordAdmin.search_fields, "=turn__id", "=source__id")
    same_case_relations = ("turn", "turn__chat", "source")
    fields = ("id", "case", "turn", "source", "start", "end")

    def get_queryset(self, request):
        return super().get_queryset(request).filter(source__turn_id=F("turn_id"))


@admin.register(ImportantItem)
class ImportantItemAdmin(CoreRecordAdmin):
    list_display = ("id", "case", "evidence_kind", "marked_at")
    list_filter = ("marked_at",)
    same_case_relations = (
        "finding_result",
        "finding_result__run",
        "chat_turn",
        "chat_turn__chat",
    )
    fields = (
        "id",
        "case",
        "evidence_kind",
        "finding_result",
        "chat_turn",
        "item_index",
        "marked_by",
        "marked_at",
    )


@admin.register(ImportantNote)
class ImportantNoteAdmin(CoreRecordAdmin):
    case_lookup = "item__case"
    list_display = ("id", "item", "created_at")
    list_filter = ("created_at",)
    search_fields = ("=id", "=item__id", "item__case__name")
    same_case_relations = (
        "item__finding_result",
        "item__finding_result__run",
        "item__chat_turn",
        "item__chat_turn__chat",
    )
    fields = ("id", "item", "author", "created_at")


@admin.register(SavedSelection)
class SavedSelectionAdmin(CoreRecordAdmin):
    list_display = ("name", "case", "revision", "updated_at")
    list_filter = ("updated_at",)
    search_fields = (*CoreRecordAdmin.search_fields, "name")
    fields = (
        "id",
        "case",
        "name",
        "revision",
        "created_by",
        "updated_by",
        "created_at",
        "updated_at",
    )


@admin.register(CaseSearchIndex)
class CaseSearchIndexAdmin(CoreRecordAdmin):
    list_display = ("id", "case", "status", "failures")
    search_fields = (*CoreRecordAdmin.search_fields, "=status", "=error_code")
    fields = (
        "id",
        "case",
        "requested_by",
        "profile",
        "status",
        "source_type_index",
        "failures",
        "error_code",
    )


@admin.register(SearchDocument)
class SearchDocumentAdmin(CoreRecordAdmin):
    list_display = ("id", "case", "source_kind", "complete")
    list_filter = ("complete",)
    same_case_relations = (
        *SOURCE_FIELDS,
        "message__sender",
        "message__recipient",
        "attachment__phone_call",
        "attachment__facetime",
        "extraction__result",
        "extraction__result__run",
        "finding__result",
        "finding__result__run",
    )
    fields = ("id", "case", "profile", "offset", "complete", *SOURCE_FIELDS)

    @admin.display(description="Source type")
    def source_kind(self, obj):
        return obj.source()[0].capitalize()


@admin.register(SearchPassage)
class SearchPassageAdmin(CoreRecordAdmin):
    list_display = ("id", "case", "start", "length")
    search_fields = (*CoreRecordAdmin.search_fields, "=document__id")
    same_case_relations = ("document",)
    fields = ("id", "case", "document", "start", "length", "digest", "profile")


@admin.register(SearchParent)
class SearchParentAdmin(CoreRecordAdmin):
    list_display = ("id", "case", "label", "version")
    search_fields = (*CoreRecordAdmin.search_fields, "label")
    same_case_relations = (
        *PARENT_ORIGINS,
        "attachment__phone_call",
        "attachment__facetime",
        "extraction__result",
        "extraction__result__run",
        "summary__run",
        "session__grouping",
    )
    fields = ("id", "case", "label", "version", "profile", *PARENT_ORIGINS)


@admin.register(SearchParentScan)
class SearchParentScanAdmin(CoreRecordAdmin):
    list_display = ("id", "case", "version", "phase")
    fields = ("id", "case", "version", "phase")
