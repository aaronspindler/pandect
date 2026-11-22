"""Bounded, source-validated saved findings beside retained conversations."""

from urllib.parse import urlencode
from uuid import UUID

from django.contrib.postgres.expressions import ArraySubquery
from django.core.paginator import Paginator
from django.db.models import (
    Count,
    Exists,
    F,
    OuterRef,
    Q,
    Subquery,
    Window,
)
from django.db.models.functions import JSONObject
from django.http import Http404

from core.analysis import ranking
from core.analysis.catalogue import (
    TIMING_ONLY_FINDINGS,
    finding_queryset,
    finding_rows,
    raw_finding_item,
)
from core.analysis.presentation import participant_text
from core.analysis.projection import is_ai_suggestion
from core.evidence.types import KIND_LABELS
from core.models import AnalysisFindingSource

PREVIEW_COUNT = 3
PER_PAGE = 25
CONTEXT_PARAMETERS = (
    "page",
    "message",
    "list_page",
    "analysis_page",
    "conversation_analysis_page",
    "weak",
)


def _context_url(params, anchor, **changes):
    query = {key: params[key] for key in CONTEXT_PARAMETERS if params.get(key)}
    query.update(changes)
    return (
        "?" + urlencode({key: value for key, value in query.items() if value}) + anchor
    )


def _finding_selection(case, messages, *, show_weak=False):
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
    base = finding_queryset(case, {"weak": "1"}).alias(citation_count=Subquery(counts))
    inline = Q(citation_count=1) & ~Q(result__run__operation="sessions")
    timing_only_count = base.filter(Exists(relevant), TIMING_ONLY_FINDINGS).count()
    if not show_weak:
        base = base.exclude(TIMING_ONLY_FINDINGS)
    findings = base.filter(Exists(relevant))
    # Inline callers join the already authorized visible message IDs. Repeating
    # the entire conversation scope here makes PostgreSQL replay its source joins.
    return findings, base.filter(inline), findings.exclude(inline), timing_only_count


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
    list_page=1,
):
    """Resolve displayed findings together; never analyze evidence while reading."""
    params = params or {}
    params = {key: params[key] for key in CONTEXT_PARAMETERS if params.get(key)}
    # Use the already authorized objects rather than propagating unvalidated values.
    params["list_page"] = list_page if list_page > 1 else ""
    show_weak = params.get("weak") == "1"
    params["weak"] = "1" if show_weak else ""
    findings, inline, shared, timing_only_count = _finding_selection(
        case, messages, show_weak=show_weak
    )
    visible_ids = {item.pk for item in visible_messages}
    previews = {}
    reasons = {}

    # Each limited subquery sees all eligible findings for its message, so the
    # preview and the expanded list pick exactly the same representatives.
    visible_inline = inline.filter(
        sources__case=case,
        sources__kind="text-message",
        sources__evidence_id__in=visible_ids,
    ).alias(message_id=F("sources__evidence_id"))
    # An unopened message may reuse a pooled preview only when every visible
    # identifier fits its lookup budget. Otherwise its link loads its own ranking.
    visible_keys = (
        visible_inline.exclude(match_digest="")
        .order_by()
        .values("match_kind", "match_version", "match_digest")
        .distinct()
    )
    load_panels_on_open = visible_keys[ranking.MAX_MATCH_KEYS :].exists()
    if load_panels_on_open:
        visible_inline = (
            visible_inline.filter(message_id=selected.pk)
            if selected is not None
            else visible_inline.none()
        )
    context = ranking.review_context(visible_inline, case)

    def message_findings(message_id):
        eligible = visible_inline.filter(message_id=message_id)
        return ranking.rank_findings(eligible, case, context=context)

    preview_rows = (
        message_findings(OuterRef("pk"))
        .annotate(finding_count=Window(Count("pk")))
        .values(row=JSONObject(id="pk", count="finding_count", reason="review_reason"))[
            :PREVIEW_COUNT
        ]
    )
    for message_id, rows in (
        messages.filter(pk__in=visible_ids)
        .annotate(finding_preview=ArraySubquery(preview_rows))
        .values_list("pk", "finding_preview")
    ):
        preview_ids = []
        for row in rows or ():
            finding_id = UUID(row["id"])
            preview_ids.append(finding_id)
            reasons[finding_id] = row["reason"]
        previews[message_id] = {
            "count": rows[0]["count"] if rows else 0,
            "finding_ids": preview_ids,
        }
    if previews.keys() != visible_ids:
        raise Http404("Messages changed. Reopen the conversation and try again.")
    selected_page = None
    if selected is not None and params.get("analysis_page"):
        if selected.pk not in previews:
            raise Http404("Message unavailable. Reopen the conversation and try again.")
        selected_page = Paginator(message_findings(selected.pk), PER_PAGE).get_page(
            params["analysis_page"]
        )
        expanded = dict(selected_page.object_list.values_list("pk", "review_reason"))
        reasons.update(expanded)
        previews[selected.pk]["finding_ids"] = list(expanded)
    shared_page_number = params.get("conversation_analysis_page")
    shared_page = Paginator(ranking.rank_findings(shared, case), PER_PAGE).get_page(
        shared_page_number
    )
    shared_selection = shared_page.object_list
    if not shared_page_number:
        shared_selection = shared_selection[:PREVIEW_COUNT]
    shared_reasons = dict(shared_selection.values_list("pk", "review_reason"))
    reasons.update(shared_reasons)
    shared_ids = list(shared_reasons)
    displayed_ids = set(shared_ids)
    for item in previews.values():
        displayed_ids.update(item["finding_ids"])
    displayed = list(
        findings.filter(pk__in=displayed_ids)
        .annotate(
            excerpt=F("preview"),
            run_id=F("result__run_id"),
            operation=F("result__run__operation"),
            raw_item=raw_finding_item(),
        )
        .only("id", "case_id", "result_id", "item_index", "label", "weak", "preview")
    )
    if {finding.pk for finding in displayed} != displayed_ids:
        raise Http404("Findings changed. Reopen the conversation and try again.")
    rows = finding_rows(case, displayed, show_weak=show_weak) if displayed else []
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
        row["review_reason"] = "" if row["unavailable"] else reasons[finding.pk]
        text = raw.get("text", "") if not row["unavailable"] else ""
        text = text.strip() if isinstance(text, str) else ""
        if row["ai_suggestion"]:
            text = participant_text(
                text,
                row.get("participant_names", {}),
                operation=finding.operation,
            )
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
            "load_on_open": load_panels_on_open and message != selected,
            "count": preview["count"],
            "rows": message_rows,
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
        "rows": shared_rows,
        "page_obj": shared_page if shared_page_number else None,
        "more_url": _context_url(
            params, "#conversation-analysis", conversation_analysis_page=1
        )
        if not shared_page_number and shared_page.paginator.count > PREVIEW_COUNT
        else "",
        **_page_links(
            shared_page,
            params,
            "conversation_analysis_page",
            "#conversation-analysis",
        ),
    }
    return {
        "conversation_analysis": conversation_analysis,
        "conversation_timing_only_count": timing_only_count,
        "show_timing_only_findings": show_weak,
        "finding_visibility_url": _context_url(
            params,
            "#conversation-finding-visibility",
            weak="" if show_weak else "1",
            analysis_page="",
            conversation_analysis_page=1,
        ),
    }
