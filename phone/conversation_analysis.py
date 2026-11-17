"""Bounded, source-validated saved findings beside retained conversations."""

from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.db.models import (
    Count,
    Exists,
    F,
    Func,
    JSONField,
    OuterRef,
    Q,
    Subquery,
    TextField,
    Value,
    Window,
)
from django.db.models.functions import Cast, RowNumber
from django.http import Http404

from core.analysis.catalogue import finding_queryset, finding_rows
from core.analysis.projection import is_ai_suggestion
from core.evidence.types import KIND_LABELS
from core.models import AnalysisFindingSource

PREVIEW_COUNT = 3
PER_PAGE = 25
SHARED_OPERATIONS = ("sessions", "session_summaries", "comm_patterns")
CONTEXT_PARAMETERS = (
    "source",
    "page",
    "message",
    "list_page",
    "analysis_page",
    "conversation_analysis_page",
)


def _context_url(params, anchor, **changes):
    query = {key: params[key] for key in CONTEXT_PARAMETERS if params.get(key)}
    query.update(changes)
    return (
        "?" + urlencode({key: value for key, value in query.items() if value}) + anchor
    )


def _finding_selection(case, messages):
    references = AnalysisFindingSource.objects.filter(finding_id=OuterRef("pk"))
    relevant = references.filter(
        case=case,
        kind="text-message",
        evidence_id__in=messages.order_by().values("pk"),
    )
    counts = (
        references.order_by()
        .values("finding_id")
        .annotate(total=Count("source_id", distinct=True))
        .values("total")
    )
    base = (
        finding_queryset(case, {})
        .exclude(result__run__operation="comm_patterns", label="Selected message")
        .alias(citation_count=Subquery(counts))
    )
    inline = Q(citation_count=1) & ~Q(result__run__operation__in=SHARED_OPERATIONS)
    findings = base.filter(Exists(relevant))
    # Inline callers join the already authorized visible message IDs. Repeating
    # the entire conversation scope here makes PostgreSQL replay its source joins.
    return findings, base.filter(inline), findings.exclude(inline)


def _page_links(page, params, parameter, anchor, **changes):
    return {
        "previous_url": _context_url(
            params, anchor, **changes, **{parameter: page.previous_page_number()}
        )
        if page.has_previous()
        else "",
        "next_url": _context_url(
            params, anchor, **changes, **{parameter: page.next_page_number()}
        )
        if page.has_next()
        else "",
    }


def conversation_analysis_context(
    case,
    messages,
    visible_messages,
    *,
    selected=None,
    params=None,
    source=None,
    list_page=1,
):
    """Resolve displayed findings together; never analyze evidence while reading."""
    params = params or {}
    params = {key: params[key] for key in CONTEXT_PARAMETERS if params.get(key)}
    # Use the already authorized objects rather than propagating unvalidated values.
    params["source"] = str(source.pk) if source else ""
    params["list_page"] = list_page if list_page > 1 else ""
    findings, inline, shared = _finding_selection(case, messages)
    visible_ids = {item.pk for item in visible_messages}
    if (
        set(messages.filter(pk__in=visible_ids).values_list("pk", flat=True))
        != visible_ids
    ):
        raise Http404("Messages changed. Reopen the conversation and try again.")
    previews = {pk: {"count": 0, "finding_ids": []} for pk in visible_ids}
    # Rank the visible messages' findings together. An outer query per message
    # repeats the latest-result/source-count work and can produce costly plans.
    preview_rows = (
        inline.filter(
            sources__case=case,
            sources__kind="text-message",
            sources__evidence_id__in=visible_ids,
        )
        .annotate(
            message_id=F("sources__evidence_id"),
            finding_count=Window(Count("pk"), partition_by=F("sources__evidence_id")),
            finding_rank=Window(
                RowNumber(),
                partition_by=F("sources__evidence_id"),
                order_by=(
                    F("result__run__created_at").desc(),
                    F("result__run_id").desc(),
                    F("item_index").asc(),
                    F("pk").asc(),
                ),
            ),
        )
        .filter(finding_rank__lte=PREVIEW_COUNT)
        .order_by("message_id", "finding_rank")
        .values("pk", "message_id", "finding_count")
    )
    for row in preview_rows:
        preview = previews[row["message_id"]]
        preview["count"] = row["finding_count"]
        preview["finding_ids"].append(row["pk"])
    selected_page = None
    if selected is not None and params.get("analysis_page"):
        if selected.pk not in previews:
            raise Http404("Message unavailable. Reopen the conversation and try again.")
        selected_page = Paginator(
            inline.filter(
                sources__case=case,
                sources__kind="text-message",
                sources__evidence_id=selected.pk,
            ),
            PER_PAGE,
        ).get_page(params["analysis_page"])
        previews[selected.pk]["finding_ids"] = list(
            selected_page.object_list.values_list("pk", flat=True)
        )
    shared_page = Paginator(shared, PER_PAGE).get_page(
        params.get("conversation_analysis_page")
    )
    shared_ids = list(shared_page.object_list.values_list("pk", flat=True))
    displayed_ids = set(shared_ids)
    for item in previews.values():
        displayed_ids.update(item["finding_ids"])
    displayed = list(
        findings.filter(pk__in=displayed_ids)
        .annotate(
            excerpt=F("preview"),
            run_id=F("result__run_id"),
            operation=F("result__run__operation"),
            raw_item=Func(
                F("result__data"),
                Value("items"),
                Cast(F("item_index"), TextField()),
                function="jsonb_extract_path",
                output_field=JSONField(),
            ),
        )
        .only("id", "case_id", "result_id", "item_index", "label", "weak", "preview")
    )
    if {finding.pk for finding in displayed} != displayed_ids:
        raise Http404("Findings changed. Reopen the conversation and try again.")
    rows = finding_rows(case, displayed) if displayed else []
    cited_message_ids = {
        item["id"]
        for row in rows
        for item in row["sources"]
        if item["kind"] == KIND_LABELS["text-message"]
    }
    cited_messages = set(
        messages.filter(pk__in=cited_message_ids).values_list("pk", flat=True)
    )
    row_by_id = {}
    for finding, row in zip(displayed, rows, strict=True):
        raw = finding.raw_item
        if not isinstance(raw, dict) or not isinstance(raw.get("values", {}), dict):
            row.update(
                unavailable=True,
                label="Finding unavailable",
                preview="",
                excerpt="",
                sources=[],
            )
        row["ai_suggestion"] = not row["unavailable"] and is_ai_suggestion(
            finding.operation, raw
        )
        text = raw.get("text", "") if not row["unavailable"] else ""
        text = text.strip() if isinstance(text, str) else ""
        row["text"] = text[:1000] or row["preview"]
        row["text_truncated"] = len(text) > 1000
        message_sources = [
            {
                "title": item["title"],
                "url": _context_url(
                    params,
                    f"#message-{item['id']}",
                    message=str(item["id"]),
                    analysis_page="",
                ),
                "archived": item["archived"],
            }
            for item in row["sources"]
            if item["kind"] == KIND_LABELS["text-message"]
            and item["id"] in cited_messages
        ]
        row["outside_sources"] = len(message_sources) < len(row["sources"])
        row["message_sources"] = message_sources[:PREVIEW_COUNT]
        row["additional_message_sources"] = max(0, len(message_sources) - PREVIEW_COUNT)
        row_by_id[finding.pk] = row
    for message in visible_messages:
        preview = previews[message.pk]
        message_rows = [row_by_id[pk] for pk in preview["finding_ids"]]
        anchor = f"#message-analysis-{message.pk}"
        paginated = selected_page if selected and selected.pk == message.pk else None
        message.analysis = {
            "count": preview["count"],
            "preview": message_rows[0]["preview"] or message_rows[0]["label"]
            if message_rows
            else "",
            "rows": message_rows,
            "open": paginated is not None,
            "page_obj": paginated,
            "more_url": _context_url(
                params, anchor, message=str(message.pk), analysis_page=1
            )
            if paginated is None and preview["count"] > PREVIEW_COUNT
            else "",
            "previous_url": "",
            "next_url": "",
        }
        if paginated is not None:
            paginated.object_list = message_rows
            message.analysis.update(
                _page_links(
                    paginated,
                    params,
                    "analysis_page",
                    anchor,
                    message=str(message.pk),
                )
            )
    shared_rows = [row_by_id[pk] for pk in shared_ids]
    shared_page.object_list = shared_rows
    conversation_analysis = {
        "count": shared_page.paginator.count,
        "preview": shared_rows[0]["preview"] or shared_rows[0]["label"]
        if shared_rows
        else "",
        "rows": shared_rows,
        "open": bool(params.get("conversation_analysis_page")),
        "page_obj": shared_page,
        "more_url": "",
        **_page_links(
            shared_page,
            params,
            "conversation_analysis_page",
            "#conversation-analysis",
        ),
    }
    return {
        "conversation_analysis": conversation_analysis,
        "has_conversation_findings": bool(conversation_analysis["count"])
        or findings.exists(),
    }
