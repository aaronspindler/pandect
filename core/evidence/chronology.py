"""Read-only case chronology of reported times and source-backed interpretations."""

from django import forms
from django.core.paginator import Paginator
from django.db.models import CharField, F, Q, Value
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_safe

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.evidence.daily_chronology import daily_chronology
from core.evidence.queries import local_date_range, person_evidence, scoped_evidence
from core.evidence.source_clocks import (
    attach_clock_details,
    attach_unplaced_dates,
    is_date_only_message,
    timestamp_fields,
    timestamp_records,
)
from core.evidence.time_claims import time_claims_page
from core.evidence.types import KIND_LABELS, KINDS
from geo.chronology import attach_candidate_journeys
from people.models import Person
from vehicle.models import Vehicle


class SourceForm(forms.Form):
    type = forms.ChoiceField(
        label="Source type",
        choices=(("", "All types"), *KIND_LABELS.items()),
        required=False,
    )
    person = forms.ModelChoiceField(
        queryset=Person.objects.none(), required=False, empty_label="All people"
    )

    def __init__(self, *args, case, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["person"].queryset = Person.objects.filter(case=case)


class ChronologyForm(SourceForm):
    category = forms.ChoiceField(
        choices=(
            ("observed", "Reported events"),
            ("acquired", "Acquisition times"),
            ("all", "Events and acquisitions"),
        ),
        required=False,
    )
    vehicle = forms.ModelChoiceField(
        queryset=Vehicle.objects.none(), required=False, empty_label="All vehicles"
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

    field_order = ("category", "type", "person", "vehicle", "start", "end")

    def __init__(self, *args, case, **kwargs):
        super().__init__(*args, case=case, **kwargs)
        self.fields["vehicle"].queryset = Vehicle.objects.filter(case=case)

    def clean(self):
        values = super().clean()
        values["category"] = values.get("category") or "observed"
        if (
            values.get("start")
            and values.get("end")
            and values["start"] > values["end"]
        ):
            self.add_error("end", "Choose an end date on or after the start date.")
        return values


def _filtered_sources(case, filters):
    for kind in KINDS:
        if filters.get("type") and kind != filters["type"]:
            continue
        queryset = scoped_evidence(case, kind).filter(is_archived=False)
        if filters.get("person"):
            queryset = person_evidence(queryset, filters["person"])
        if filters.get("vehicle"):
            if kind != "vehicle-location":
                continue
            queryset = queryset.filter(vehicle=filters["vehicle"])
        yield kind, queryset


def chronology_records(case, filters):
    date_range = local_date_range(
        "event_at", start=filters.get("start"), end=filters.get("end")
    )
    queries = []
    for kind, queryset in _filtered_sources(case, filters):
        for field, label in timestamp_fields(kind, filters["category"]):
            events = timestamp_records(queryset, case, kind, field).annotate(
                event_at=F(field),
                event_label=Value(label, output_field=CharField()),
                kind=Value(kind, output_field=CharField()),
            )
            queries.append(
                events.filter(**date_range)
                .order_by()
                .values("id", "title", "kind", "event_at", "event_label")
            )
    if not queries:
        return []
    return (
        queries[0]
        .union(*queries[1:], all=True)
        .order_by("event_at", "kind", "id", "event_label")
    )


def _undated_originals(case, filters):
    queries = []
    for kind, queryset in _filtered_sources(case, filters):
        for field, _ in timestamp_fields(kind, "observed"):
            unplaced = Q(**{field + "__isnull": True})
            if kind == "text-message":
                unplaced |= Q(is_date_only_message(case))
            queryset = queryset.filter(unplaced)
        queries.append(
            queryset.order_by().annotate(kind=Value(kind)).values("id", "title", "kind")
        )
    return queries[0].union(*queries[1:], all=True).order_by("title", "kind", "id")


def _link_originals(case, rows, person):
    params = {"person": person.pk} if person else None
    for row in rows:
        row["label"] = KIND_LABELS[row["kind"]]
        row["url"] = reverse(
            "core:evidence:detail", args=[case.pk, row["kind"], row["id"]], query=params
        )


def _unplaced(request, case, *, references):
    form = SourceForm(request.GET, case=case)
    context = {
        **case_context(case, request.user),
        "nav_section": "chronology",
        "form": form,
        "references": references,
        "view_name": "references" if references else "unplaced",
    }
    valid = form.is_valid()
    if valid:
        filters = form.cleaned_data
        context["claims"] = time_claims_page(
            case, filters, request.GET.get("page"), dates_only=references
        )
        if not references:
            originals = Paginator(_undated_originals(case, filters), 25).get_page(
                request.GET.get("originals_page")
            )
            originals.object_list = list(originals.object_list)
            _link_originals(case, originals, filters["person"])
            attach_unplaced_dates(case, originals.object_list)
            context["originals"] = originals
    return render(
        request, "core/evidence/unplaced.html", context, status=200 if valid else 400
    )


@product_login_required
@require_safe
def chronology(request, case_id):
    case = get_case(request.user, case_id)
    view = request.GET.get("view")
    if view == "day":
        return daily_chronology(request, case)
    if view in {"references", "unplaced"}:
        return _unplaced(request, case, references=view == "references")
    account_timezone = timezone.get_current_timezone_name()
    show_utc = request.GET.get("clock") == "utc"
    params = request.GET.copy()
    if not params.get("category"):
        params["category"] = "observed"
    form = ChronologyForm(params, case=case)
    valid = form.is_valid()
    records = chronology_records(case, form.cleaned_data) if valid else []
    page = Paginator(records, 25).get_page(request.GET.get("page"))
    rows = list(page.object_list)
    if rows:
        _link_originals(case, rows, form.cleaned_data.get("person"))
        attach_clock_details(case, rows)
        attach_candidate_journeys(case, rows)
    return render(
        request,
        "core/evidence/chronology.html",
        {
            **case_context(case, request.user),
            "nav_section": "chronology",
            "form": form,
            "rows": rows,
            "page_obj": page,
            "display_timezone": "UTC" if show_utc else account_timezone,
            "account_timezone": account_timezone,
            "show_utc": show_utc,
            "filters_applied": params["category"] != "observed"
            or any(
                params.get(key) for key in ("type", "person", "vehicle", "start", "end")
            ),
        },
        status=200 if valid else 400,
    )
