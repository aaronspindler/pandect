"""Read-only chronology of explicit source timestamps, never inferred events."""

from urllib.parse import urlencode

from django import forms
from django.core.paginator import Paginator
from django.db.models import CharField, F, Value
from django.http import QueryDict
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_safe

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.analysis.catalogue import catalogue_page
from core.evidence.queries import person_evidence, scoped_evidence
from core.evidence.types import COMMUNICATION_KINDS, KIND_LABELS, KINDS, LOCATION_KINDS
from people.models import Person
from vehicle.models import Vehicle


class ChronologyForm(forms.Form):
    category = forms.ChoiceField(
        choices=(
            ("observed", "Observed events"),
            ("acquired", "Acquisition times"),
            ("all", "Events and acquisitions"),
        ),
        required=False,
    )
    type = forms.ChoiceField(
        label="Source type",
        choices=(("", "All types"), *KIND_LABELS.items()),
        required=False,
    )
    person = forms.ModelChoiceField(
        queryset=Person.objects.none(), required=False, empty_label="All people"
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

    def __init__(self, *args, case, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["person"].queryset = Person.objects.filter(case=case)
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


def chronology_records(case, filters):
    queries = []
    for kind in KINDS:
        if filters.get("type") and kind != filters["type"]:
            continue
        queryset = scoped_evidence(case, kind)
        if filters.get("person"):
            queryset = person_evidence(queryset, filters["person"])
        if filters.get("vehicle"):
            if kind != "vehicle-location":
                continue
            queryset = queryset.filter(vehicle=filters["vehicle"])
        timestamps = []
        if filters["category"] in ("all", "observed"):
            if kind in COMMUNICATION_KINDS:
                timestamps.append(("occurred_at", "Occurred"))
            elif kind in LOCATION_KINDS:
                timestamps.append(("observed_at", "Observed"))
        if filters["category"] in ("all", "acquired"):
            timestamps.append(("collected_at", "Collected"))
        for field, label in timestamps:
            events = queryset.filter(**{f"{field}__isnull": False}).annotate(
                event_at=F(field),
                event_label=Value(label, output_field=CharField()),
                kind=Value(kind, output_field=CharField()),
            )
            # Django's datetime date transform uses the active user timezone.
            if filters.get("start"):
                events = events.filter(event_at__date__gte=filters["start"])
            if filters.get("end"):
                events = events.filter(event_at__date__lte=filters["end"])
            queries.append(
                events.order_by().values(
                    "id", "title", "is_archived", "kind", "event_at", "event_label"
                )
            )
    if not queries:
        return []
    return (
        queries[0]
        .union(*queries[1:], all=True)
        .order_by("event_at", "kind", "id", "event_label")
    )


@product_login_required
@require_safe
def chronology(request, case_id):
    case = get_case(request.user, case_id)
    params = request.GET.copy()
    if not params.get("category"):
        params["category"] = "observed"
    form = ChronologyForm(params, case=case)
    valid = form.is_valid()
    records = chronology_records(case, form.cleaned_data) if valid else []
    page = Paginator(records, 25).get_page(request.GET.get("page"))
    rows = list(page.object_list)
    person = form.cleaned_data.get("person") if valid else None
    for row in rows:
        row["label"] = KIND_LABELS[row["kind"]]
        source_params = {"state": "all"}
        if person:
            source_params["person"] = str(person.pk)
        row["url"] = (
            reverse("core:evidence:detail", args=[case.pk, row["kind"], row["id"]])
            + "?"
            + urlencode(source_params)
        )
    unplaced = None
    # A vehicle filter has no text date-candidate source: never show unrelated dates.
    if valid and not form.cleaned_data.get("vehicle"):
        finding_params = QueryDict(mutable=True)
        finding_params.update(
            {
                "method": "entities",
                "kind": form.cleaned_data.get("type", ""),
                "unplaced_page": request.GET.get("unplaced_page", "1"),
            }
        )
        unplaced = catalogue_page(
            case,
            finding_params,
            person=person,
            page_parameter="unplaced_page",
            labels=("Date candidate",),
        )
    return render(
        request,
        "core/evidence/chronology.html",
        {
            **case_context(case, request.user),
            "nav_section": "chronology",
            "form": form,
            "rows": rows,
            "page_obj": page,
            "unplaced": unplaced,
            "display_timezone": timezone.get_current_timezone_name(),
            "filters_open": not valid
            or params["category"] != "observed"
            or any(
                params.get(key) for key in ("type", "person", "vehicle", "start", "end")
            ),
        },
        status=200 if valid else 400,
    )
