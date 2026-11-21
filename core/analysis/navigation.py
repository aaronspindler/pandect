"""Finding pages and source links share one zero-based index convention."""

from urllib.parse import parse_qsl, urlencode
from uuid import UUID

from django.urls import reverse

from core.evidence.navigation import collection_params
from core.evidence.types import KIND_LABELS
from core.models import AnalysisRun

FINDINGS_PER_PAGE = 50
FINDINGS_LIST_FIELDS = (
    "q",
    "method",
    "kind",
    "evidence",
    "person",
    "history",
    "weak",
    "page",
)


def history_filters(query):
    return {
        key: query[key]
        for key, choices in (
            ("kind", KIND_LABELS),
            ("status", AnalysisRun.Status.values),
        )
        if query.get(key) in choices
    }


def history_query(query):
    params = history_filters(query)
    if query.get("reviews") == "messages":
        params["reviews"] = "messages"
    if query.get("history") == "1":
        params["history"] = "1"
    page = query.get("history_page", "")
    if page.isascii() and page.isdecimal() and len(page) <= 9 and int(page) > 1:
        params["history_page"] = page
    batch = query.get("batch", "")
    if len(batch) == 36:
        try:
            params["batch"] = str(UUID(batch))
        except ValueError:
            pass
    return params


def processing_back_query(history, *, batch_id=None):
    """Restore a validated detail query to the Processing list."""
    params = history.copy()
    page = params.pop("history_page", None)
    if page:
        params["page"] = page
    if params.get("batch") != str(batch_id):
        params.pop("batch", None)
    return params


def query_suffix(params):
    return "?" + urlencode(params) if params else ""


def findings_list_query(query):
    """Restore a catalogue selection through its case-bound route."""
    encoded = query.get("return_findings")
    if encoded is None or len(encoded) > 3000:
        return None
    try:
        values = dict(parse_qsl(encoded, max_num_fields=len(FINDINGS_LIST_FIELDS)))
    except ValueError:
        return None
    return {key: values[key] for key in FINDINGS_LIST_FIELDS if key in values}


def evidence_return_url(case_id, query):
    """Restore the reader through its case-bound route and an exact row anchor."""
    encoded = query.get("return_evidence", "")
    if not encoded or len(encoded) > 3000:
        return ""
    try:
        selection = dict(parse_qsl(encoded, max_num_fields=16))
        evidence_id = UUID(selection["evidence"])
        kind = selection["kind"]
        anchor = (
            f"evidence-finding-{UUID(selection['row'])}"
            if "row" in selection
            else "evidence-layout"
            if selection.get("layout") == "1"
            else "evidence-history-heading"
        )
    except KeyError, ValueError:
        return ""
    if kind not in KIND_LABELS:
        return ""
    params = collection_params(selection)
    for key in ("findings_page", "results_page"):
        if key in selection:
            params[key] = selection[key]
    return reverse(
        "core:evidence:detail",
        args=[case_id, kind, evidence_id],
        query=params,
        fragment=anchor,
    )


def result_navigation(query, case_id):
    params = history_query(query)
    catalogue = findings_list_query(query)
    if catalogue is not None:
        params["return_findings"] = urlencode(catalogue)
    if evidence_return_url(case_id, query):
        params["return_evidence"] = query["return_evidence"]
    return params


def finding_url(case_id, run_id, finding_index, *, show_weak=False, history=None):
    url = reverse("core:analysis:detail", kwargs={"case_id": case_id, "run_id": run_id})
    params = {"weak": 1} if show_weak else {}
    params.update(
        page=finding_index // FINDINGS_PER_PAGE + 1, finding=finding_index + 1
    )
    params.update(history or {})
    return f"{url}{query_suffix(params)}#finding-{finding_index + 1}"
