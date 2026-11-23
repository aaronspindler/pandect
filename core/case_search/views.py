"""Authorized case-wide search and semantic index recovery."""

from datetime import date, datetime, time, timedelta

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import (
    require_http_methods,
    require_POST,
    require_safe,
)

from cases.permissions import case_context, get_case
from config.access import product_login_required

from .approval import search_approval_current
from .indexing import prepare_case_search, search_index_status
from .queries import highlight_terms, search, suggest
from .semantic import meaning_query, related_results
from .semantic_model import is_external, profile
from .sources import TYPE_CHOICES


class SearchForm(forms.Form):
    q = forms.CharField(
        label="Search this case",
        required=False,
        max_length=200,
        widget=forms.TextInput(attrs={"id": "case-search-query"}),
    )
    type = forms.ChoiceField(
        label="Record type",
        required=False,
        choices=[("", "All record types"), *TYPE_CHOICES],
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
    state = forms.ChoiceField(
        label="Evidence state",
        required=False,
        choices=(
            ("active", "Active evidence"),
            ("all", "Include archived evidence"),
            ("archived", "Archived evidence only"),
        ),
    )
    sort = forms.ChoiceField(
        label="Sort keyword results",
        required=False,
        choices=(
            ("relevance", "Relevance"),
            ("newest", "Newest first"),
            ("oldest", "Oldest first"),
        ),
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

    def active_filters(self):
        values = self.cleaned_data
        labels = []
        for key, default in (("type", ""), ("state", "active"), ("sort", "relevance")):
            value = values.get(key)
            if value and value != default:
                labels.append(dict(self.fields[key].choices)[value])
        if values.get("start"):
            labels.append(f"From {values['start']:%Y-%m-%d}")
        if values.get("end"):
            labels.append(f"Through {values['end']:%Y-%m-%d}")
        return labels


@product_login_required
@require_http_methods(["GET", "HEAD", "POST"])
def results(request, case_id):
    case = get_case(request.user, case_id)
    form = SearchForm(request.GET)
    valid = form.is_valid()
    context = case_context(case, request.user) | {
        "search_form": form,
        "case_search_query": request.GET.get("q", ""),
        "display_timezone": timezone.get_current_timezone_name(),
        "active_filters": form.active_filters(),
    }
    if valid and (query := form.cleaned_data["q"]):
        values = form.cleaned_data
        start, end = values["start"], values["end"]
        filters = {
            "record_type": values["type"],
            "state": values["state"] or "active",
            "start": timezone.make_aware(datetime.combine(start, time.min))
            if start
            else None,
            "end": timezone.make_aware(
                datetime.combine(end + timedelta(days=1), time.min)
            )
            if end and end < date.max
            else None,
        }
        try:
            terms = highlight_terms(query)
            page = search(
                case,
                query=query,
                **filters,
                sort=values["sort"] or "relevance",
                page=request.GET.get("page", 1),
            )
        except ValidationError as error:
            form.add_error("q", error)
        else:
            context.update({"page_obj": page, "terms": terms})
            if page.number == 1:
                if not page.paginator.count:
                    _, context["spelling"] = suggest(case, query)
                if meaning_query(query, values["type"]):
                    submitted = (
                        request.method == "POST" and request.POST.get("meaning") == "1"
                    )
                    local_link = (
                        request.method == "GET"
                        and request.GET.get("meaning") == "1"
                        and not is_external()
                    )
                    if submitted or local_link:
                        context["meaning_requested"] = True
                        context["related"], context["meaning_status"] = related_results(
                            case,
                            user=request.user,
                            query=query,
                            allow_external=submitted,
                            keyword_urls={row["url"] for row in page},
                            **filters,
                        )
                    else:
                        context["hosted_meaning"] = is_external()
                        context["offer_meaning"] = bool(profile()) and (
                            not context["hosted_meaning"]
                            or search_approval_current(case)
                        )
            get_case(request.user, case_id)
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
    rows = []
    if 2 <= len(query) <= 200:
        try:
            rows, _ = suggest(case, query, spelling=False)
        except ValidationError:
            pass  # Full submission provides validation beside the query field.
        get_case(request.user, case_id)
    return JsonResponse(
        {
            "results": [
                {key: row[key] for key in ("title", "label", "url")} for row in rows[:6]
            ]
        }
    )


@product_login_required
@require_POST
def retry(request, case_id):
    case = get_case(request.user, case_id, edit=True)
    if prepare_case_search(case, request.user, retry=True):
        messages.success(request, "Search indexing queued.")
    else:
        case.refresh_from_db()
        messages.info(request, search_index_status(case, count_passages=False)["label"])
    return redirect(
        reverse("core:analysis:processing", args=[case.pk]) + "#case-search-index"
    )
