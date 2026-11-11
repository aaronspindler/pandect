"""Finding pages and source links share one zero-based index convention."""

from urllib.parse import urlencode

from django.urls import reverse

from core.evidence.types import KIND_LABELS
from core.models import AnalysisRun

FINDINGS_PER_PAGE = 50


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
    page = query.get("history_page", "")
    if page.isascii() and page.isdecimal() and len(page) <= 9 and int(page) > 1:
        params["history_page"] = page
    return params


def query_suffix(params):
    return "?" + urlencode(params) if params else ""


def finding_url(case_id, run_id, finding_index, *, show_weak=False, history=None):
    url = reverse("core:analysis:detail", kwargs={"case_id": case_id, "run_id": run_id})
    params = {"weak": 1} if show_weak else {}
    params.update(
        page=finding_index // FINDINGS_PER_PAGE + 1, finding=finding_index + 1
    )
    params.update(history or {})
    return f"{url}{query_suffix(params)}#finding-{finding_index + 1}"
