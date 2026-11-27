"""Run the retained writing methods on messages attributed to one case person."""

from urllib.parse import parse_qsl
from uuid import uuid4

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import DatabaseError, transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.analysis.errors import AnalysisError, error_message
from core.analysis.services import start_run
from core.evidence.queries import local_date_range, scoped_evidence
from core.evidence.source_clocks import is_date_only_message
from core.models import AnalysisRun
from people.models import Person
from text.analysis import attributed_sources
from ui.navigation import list_query


class PersonWritingForm(forms.Form):
    first_start = forms.DateField(
        label="From", widget=forms.DateInput(attrs={"type": "date"})
    )
    first_end = forms.DateField(
        label="Through", widget=forms.DateInput(attrs={"type": "date"})
    )
    second_start = forms.DateField(
        label="From", widget=forms.DateInput(attrs={"type": "date"})
    )
    second_end = forms.DateField(
        label="Through", widget=forms.DateInput(attrs={"type": "date"})
    )
    submission_id = forms.UUIDField(initial=uuid4, widget=forms.HiddenInput)

    def __init__(self, *args, compare, **kwargs):
        super().__init__(*args, **kwargs)
        self.periods = ("first", "second") if compare else ("first",)
        if not compare:
            for field in ("first_start", "first_end"):
                self.fields[field].required = False
            del self.fields["second_start"], self.fields["second_end"]

    def clean(self):
        data = super().clean()
        for period in self.periods:
            start, end = data.get(f"{period}_start"), data.get(f"{period}_end")
            if start and end and end < start:
                self.add_error(f"{period}_end", "Enter a date on or after the start.")
        first_end, second_start = data.get("first_end"), data.get("second_start")
        if first_end and second_start and second_start <= first_end:
            self.add_error(
                "second_start", "Start the later period after the earlier period ends."
            )
        return data


def _periods(case, attributed, data, periods):
    for period in periods:
        start, end = data.get(f"{period}_start"), data.get(f"{period}_end")
        rows = attributed.filter(
            **local_date_range("occurred_at", start=start, end=end)
        )
        if start or end:
            rows = rows.filter(~is_date_only_message(case))
        yield rows


def _queue_writing(actor, case, person_id, attributed, operation, form):
    data = form.cleaned_data
    scope = {
        "person_id": str(person_id),
        "timezone": timezone.get_current_timezone_name(),
        "version": 1,
    }
    for period in form.periods:
        for boundary in ("start", "end"):
            name = f"{period}_{boundary}"
            date = data[name]
            scope[name] = date.isoformat() if date else None

    existing = AnalysisRun.objects.filter(
        case=case, requested_by=actor, submission_id=data["submission_id"]
    ).first()
    if existing:
        saved_scope = existing.parameters.get("writing_scope", {})
        if existing.operation != operation or any(
            saved_scope.get(key) != value
            for key, value in scope.items()
            if key != "version"
        ):
            raise ValidationError(
                "This submission was already used for different dates."
            )
        return existing, False

    sources = []
    for index, rows in enumerate(_periods(case, attributed, data, form.periods)):
        size_before_period = len(sources)
        remaining = attributed_sources.MAX_WRITING_MESSAGES - size_before_period
        identifiers = rows.order_by("occurred_at", "pk").values_list("pk", flat=True)[
            : remaining + 1
        ]
        sources.extend(
            {"kind": "text-message", "id": str(pk)} for pk in identifiers.iterator()
        )
        attributed_sources.check_message_count(len(sources))
        if len(sources) == size_before_period:
            if operation == "style_change":
                label = "earlier" if index == 0 else "later"
                raise ValidationError(f"No attributed messages in the {label} period.")
            raise ValidationError("No attributed messages match this selection.")
        if operation == "style_change" and index == 0:
            scope["period_split"] = len(sources)
    scope["source_count"] = len(sources)
    try:
        return start_run(
            actor,
            case.pk,
            operation,
            sources,
            parameters={"writing_scope": scope},
            submission_id=data["submission_id"],
        )
    except Http404:
        raise AnalysisError("source_unavailable") from None


def _selected_counts(case, attributed, form, compare):
    if form.is_bound:
        if not form.is_valid():
            return None
        data = form.cleaned_data
    elif compare:
        return None
    else:
        data = {}
    counts = [rows.count() for rows in _periods(case, attributed, data, form.periods)]
    return {"earlier": counts[0], "later": counts[-1], "total": sum(counts)}


@product_login_required
@require_http_methods(["GET", "HEAD", "POST"])
def writing_style(request, case_id, person_id):
    submitted = request.method == "POST"
    count_request = not submitted and request.GET.get("writing_counts") == "1"
    case = get_case(request.user, case_id, edit=submitted or count_request)
    person = get_object_or_404(Person, case=case, pk=person_id)
    attributed = scoped_evidence(case, "text-message").filter(
        sender=person, is_archived=False
    )
    compare = request.GET.get("mode") == "compare"
    operation = "style_change" if compare else "style_profiles"
    if count_request:
        data = request.GET.copy()
        data["submission_id"] = str(uuid4())
        form = PersonWritingForm(data, compare=compare)
        return render(
            request,
            "text/writing_counts.html",
            {
                "selected_counts": _selected_counts(case, attributed, form, compare),
                "compare": compare,
            },
        )
    form = PersonWritingForm(request.POST if submitted else None, compare=compare)
    queue = submitted and request.POST.get("action") != "count"
    if queue and form.is_valid():
        try:
            with transaction.atomic():
                case = get_case(request.user, case_id, edit=True, lock=True)
                run, created = _queue_writing(
                    request.user, case, person.pk, attributed, operation, form
                )
        except ValidationError as error:
            form.add_error(None, error)
        except AnalysisError as error:
            form.add_error(None, error_message(error.code))
        except DatabaseError:
            form.add_error(None, "Analysis could not be queued. Please try again.")
        else:
            messages.success(
                request,
                "Analysis queued." if created else "This analysis was already queued.",
            )
            return redirect(
                reverse(
                    "core:analysis:detail",
                    args=[case.pk, run.pk],
                    query=dict(parse_qsl(list_query(request))),
                )
            )

    runs = AnalysisRun.objects.filter(
        case=case,
        operation__in=("style_profiles", "style_change"),
        parameters__contains={"writing_scope": {"person_id": str(person.pk)}},
    ).only("operation", "status", "created_at", "parameters")
    return render(
        request,
        "text/person_writing.html",
        case_context(case, request.user)
        | {
            "person": person,
            "compare": compare,
            "form": form,
            "display_timezone": timezone.get_current_timezone_name(),
            "list_query": list_query(request),
            "runs": Paginator(runs, 25).get_page(request.GET.get("writing_page")),
            "has_messages": attributed.exists(),
            "selected_counts": _selected_counts(case, attributed, form, compare)
            if not submitted or form.is_valid()
            else None,
        },
        status=400 if submitted and (queue or not form.is_valid()) else 200,
    )
