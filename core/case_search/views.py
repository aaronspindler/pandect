"""Investigator-facing passage search and private index recovery."""

from datetime import date, datetime, time, timedelta

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST, require_safe

from cases.permissions import accessible_cases, case_context, get_case
from config.access import product_login_required

from .hosted import NOTICES, HostedSearchError, hits
from .indexing import prepare_case_search, search_index_status
from .material import kind_label
from .sources import TYPE_CHOICES


class SearchForm(forms.Form):
    q = forms.CharField(max_length=200, required=False)
    type = forms.ChoiceField(
        label="Record type",
        required=False,
        choices=[
            ("", "All records"),
            *TYPE_CHOICES,
        ],
    )
    start = forms.DateField(
        label="From date",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    end = forms.DateField(
        label="Through date",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["form"] = "case-search"

    def clean(self):
        values = super().clean()
        if (
            values.get("start")
            and values.get("end")
            and values["start"] > values["end"]
        ):
            self.add_error("end", "Choose an end date on or after the start date.")
        return values

    def filters(self):
        start, end = self.cleaned_data["start"], self.cleaned_data["end"]
        return {
            "record_type": self.cleaned_data["type"],
            "start": timezone.make_aware(datetime.combine(start, time.min))
            if start
            else None,
            "end": timezone.make_aware(
                datetime.combine(end + timedelta(days=1), time.min)
            )
            if end and end < date.max
            else None,
        }


def result_rows(case, actor, query, limit, filters=None):
    rows = []
    for found, start, text in hits(
        case, actor, query, limit=limit, filters=filters, records=True
    ):
        url = found.url
        loc = found.locator
        if (
            loc["surface"] == "original"
            and loc["kind"] == "text"
            and start + len(text) <= loc["metadata_start"]
        ):
            url = reverse(
                "core:evidence:detail",
                args=(case.pk, "text", loc["pk"]),
                query={"passage": f"{start}-{start + len(text)}"},
                fragment="cited-passage",
            )
        rows.append(
            {
                "title": loc["label"],
                "url": url,
                "label": kind_label(loc),
                "excerpt": text[:360],
            }
        )
    return rows


@product_login_required
@require_safe
def results(request, case_id):
    case = get_case(request.user, case_id)
    form = SearchForm(request.GET)
    context = case_context(case, request.user) | {
        "search_form": form,
        "case_search_query": request.GET.get("q", ""),
        "index": search_index_status(case, count_passages=False),
    }
    if form.is_valid() and (query := form.cleaned_data["q"]):
        try:
            context["rows"] = result_rows(case, request.user, query, 30, form.filters())
        except HostedSearchError as exc:
            context["notice"] = NOTICES.get(exc.code, NOTICES["search_unavailable"])
    return render(
        request,
        "core/case_search/results.html",
        context,
        status=400 if form.errors else 200,
    )


@product_login_required
@require_safe
def suggestions(request, case_id):
    case = get_case(request.user, case_id)
    query = request.GET.get("q", "").strip()
    results = []
    if 2 <= len(query) <= 200:
        try:
            results = result_rows(case, request.user, query, 6)
        except HostedSearchError:
            pass
    return JsonResponse(
        {"results": [{k: r[k] for k in ("title", "label", "url")} for r in results]}
    )


@product_login_required
@require_safe
def all_cases(request):
    query = request.GET.get("q", "").strip()
    if len(query) > 200:
        return render(
            request,
            "core/case_search/all_cases.html",
            {"error": "Use 200 characters or fewer."},
            status=400,
        )
    groups = []
    enabled = (
        accessible_cases(request.user)
        .filter(external_ai_enabled=True)
        .order_by("name", "pk")
    )
    page = Paginator(enabled, 8).get_page(request.GET.get("page"))
    if query:
        for case in page:
            try:
                rows = result_rows(case, request.user, query, 4)
                groups.append({"case": case, "rows": rows})
            except Http404, PermissionDenied:
                continue
            except HostedSearchError as exc:
                groups.append(
                    {
                        "case": case,
                        "notice": NOTICES.get(exc.code, NOTICES["search_unavailable"]),
                    }
                )
        # Membership might have changed during another case's request.
        current = set(
            accessible_cases(request.user)
            .filter(
                external_ai_enabled=True, pk__in=[group["case"].pk for group in groups]
            )
            .values_list("pk", "external_ai_revision")
        )
        groups = [
            g
            for g in groups
            if (g["case"].pk, g["case"].external_ai_revision) in current
        ]
    return render(
        request,
        "core/case_search/all_cases.html",
        {"groups": groups, "query": query, "page": page},
    )


@product_login_required
@require_POST
def retry(request, case_id):
    case = get_case(request.user, case_id, edit=True)
    try:
        queued = prepare_case_search(case, request.user, retry=True)
    except HostedSearchError as exc:
        messages.error(request, NOTICES.get(exc.code, NOTICES["search_unavailable"]))
    else:
        messages.info(
            request,
            "Search indexing queued." if queued else search_index_status(case)["label"],
        )
    return redirect(
        reverse(
            "core:analysis:processing", args=[case.pk], fragment="case-search-index"
        )
    )
