"""Read retained mentions and interpretations whose occurrence time is unknown."""

from django.core.paginator import Paginator
from django.db.models import F, Q
from django.db.models.functions import Lower, Trim

from core.analysis.catalogue import (
    PER_PAGE,
    finding_queryset,
    raw_finding_item,
    resolved_findings,
)
from core.analysis.navigation import finding_url
from core.analysis.operations import OPERATIONS

CLAIM_LABELS = {
    "plan": "Planned event",
    "commitment": "Commitment",
    "cancellation": "Cancellation",
    "reported_event": "Reported event",
    "correction": "Correction",
}


def _citations(operation, item, by_id):
    """Reproduce every exact quotation against the pinned extraction or original."""
    source_ids = item.get("source_ids")
    anchors = item.get("anchors")
    if (
        not isinstance(source_ids, list)
        or not all(isinstance(key, str) for key in source_ids)
        or not set(source_ids) <= by_id.keys()
        or not isinstance(anchors, list)
        or not 1 <= len(anchors) <= 10
    ):
        return []
    citations = []
    covered = set()
    for anchor in anchors:
        if not isinstance(anchor, dict):
            return []
        key = anchor.get("source_id")
        source = by_id.get(key) if isinstance(key, str) else None
        start, end = anchor.get("start"), anchor.get("end")
        if (
            source is None
            or type(start) is not int
            or type(end) is not int
            or not 0 <= start < end <= len(source.text)
            or end - start > 4000
            or anchor != source.anchor(start=start, end=end)
        ):
            return []
        covered.add(key)
        citations.append((source, source.text[start:end]))
    if covered != set(source_ids):
        return []
    if operation == "entities" and (
        len(citations) != 1 or citations[0][1] != item.get("text")
    ):
        return []
    return citations


def unavailable_time_claim(operation, item, by_id):
    """Verify temporal passages before showing or exporting the saved canonical item."""
    if operation not in {"entities", "content_events"}:
        return False
    if not isinstance(item, dict):
        return True
    label = item.get("label")
    if not isinstance(label, str):
        return True
    temporal = (operation == "entities" and label == "Date candidate") or (
        operation == "content_events" and label.strip().lower() in CLAIM_LABELS
    )
    return temporal and not _citations(operation, item, by_id)


def _claim_row(case, finding, run, sources):
    item = finding.raw_item
    if (
        not sources
        or not isinstance(item, dict)
        or item.get("label") != finding.label
        or item.get("report_only")
        or not isinstance(item.get("text"), str)
        or not isinstance(item.get("values"), dict)
    ):
        return {"unavailable": True}
    by_id = {source.id: source for source in sources}
    citations = _citations(run.operation, item, by_id)
    if not citations or set(item["source_ids"]) != by_id.keys():
        return {"unavailable": True}
    ai = run.operation == "content_events"
    values = item["values"]
    if ai:
        label = CLAIM_LABELS[finding.label.strip().lower()]
        status = values.get("status")
        if not isinstance(status, str):
            return {"unavailable": True}
    else:
        label = "Relative date" if values.get("relative") is True else "Date reference"
        status = values.get("interpretation", "")
    return {
        "label": label,
        "status": status,
        "text": item["text"] if ai else "",
        "uses_ai": ai,
        "partial": run.status == "partial",
        "method": OPERATIONS[run.operation].label,
        "method_version": run.method_version,
        "url": finding_url(case.pk, run.pk, finding.item_index),
        "citations": [
            {
                "quote": quote,
                "title": source.item.title,
                "url": source.original_url,
                "archived": source.item.is_archived,
            }
            for source, quote in citations
        ],
    }


def time_claims_page(case, filters, number, *, dates_only=False):
    eligible = Q(result__run__operation="entities", label="Date candidate")
    if not dates_only:
        eligible |= Q(
            result__run__operation="content_events", claim_kind__in=CLAIM_LABELS
        )
    findings = (
        finding_queryset(
            case, {"kind": filters.get("type"), "person": filters.get("person")}
        )
        .alias(claim_kind=Lower(Trim("label")))
        .filter(eligible)
        .annotate(run_id=F("result__run_id"), raw_item=raw_finding_item())
        .only("pk", "label", "item_index", "result_id")
    )
    page = Paginator(findings, PER_PAGE).get_page(number)
    page.object_list = [
        _claim_row(case, finding, run, sources)
        for finding, run, sources, _ in resolved_findings(
            case, page.object_list, with_content=True
        )
    ]
    return page
