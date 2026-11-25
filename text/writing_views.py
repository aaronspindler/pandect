"""Run the retained writing methods on messages attributed to one case person."""

from uuid import uuid4

from django import forms
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import DatabaseError, transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.analysis.errors import AnalysisError, error_message
from core.analysis.services import start_run
from core.analysis.sources import source_id
from core.evidence.queries import local_date_range, scoped_evidence
from core.models import AnalysisRun
from people.models import Person
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


def _queue_writing(actor, case, person_id, attributed, operation, form):
    data = form.cleaned_data
    scope = {
        "person_id": str(person_id),
        "timezone": timezone.get_current_timezone_name(),
    }
    parameters = {"writing_scope": scope}
    sources = []
    limit = settings.PANDECT_ANALYSIS_MAX_SOURCES
    for period, group, label in zip(
        form.periods, ("group_a", "group_b"), ("earlier", "later"), strict=False
    ):
        start, end = data[f"{period}_start"], data[f"{period}_end"]
        scope[f"{period}_start"] = start.isoformat() if start else None
        scope[f"{period}_end"] = end.isoformat() if end else None
        ids = (
            attributed.filter(**local_date_range("occurred_at", start=start, end=end))
            .order_by("occurred_at", "pk")
            .values_list("pk", flat=True)
        )
        choices = [
            {"kind": "text-message", "id": str(pk)}
            for pk in ids[: limit - len(sources) + 1]
        ]
        if not choices:
            if operation == "style_change":
                raise ValidationError(f"No attributed messages in the {label} period.")
            raise ValidationError("No attributed messages match this selection.")
        sources.extend(choices)
        if len(sources) > limit:
            raise ValidationError(
                f"The selection exceeds {limit} messages. Choose shorter periods."
            )
        if operation == "style_change":
            parameters[group] = [source_id(choice) for choice in choices]
    try:
        return start_run(
            actor,
            case.pk,
            operation,
            sources,
            parameters=parameters,
            submission_id=data["submission_id"],
        )
    except Http404:
        raise AnalysisError("source_unavailable") from None


@product_login_required
@require_http_methods(["GET", "HEAD", "POST"])
def writing_style(request, case_id, person_id):
    submitted = request.method == "POST"
    case = get_case(request.user, case_id, edit=submitted)
    person = get_object_or_404(Person, case=case, pk=person_id)
    attributed = scoped_evidence(case, "text-message").filter(
        sender=person, is_archived=False
    )
    compare = request.GET.get("mode") == "compare"
    operation = "style_change" if compare else "style_profiles"
    form = PersonWritingForm(request.POST if submitted else None, compare=compare)
    if submitted and form.is_valid():
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
            return redirect("core:analysis:detail", case_id=case.pk, run_id=run.pk)

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
        },
        status=400 if submitted else 200,
    )
