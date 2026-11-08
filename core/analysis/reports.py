"""Case-scoped validation and explicit dispatch for current-field reports."""

from django.core.exceptions import ValidationError
from django.http import Http404
from django.utils import timezone

from core.analysis.deterministic.reports import duplicate_report
from core.analysis.reporting import MAX_RECORDS
from geo.analysis.deterministic.reports import vehicle_report
from people.models import Person
from phone.analysis.deterministic.reports import call_report
from vehicle.models import Vehicle

REPORT_NAMES = {
    "calls": "calls",
    "vehicle-coverage": "vehicle",
    "vehicle-speed": "vehicle",
    "duplicates": "duplicates",
}


def report(
    case,
    name,
    *,
    state="active",
    vehicle_id=None,
    participant_id=None,
    other_participant_id=None,
    start=None,
    end=None,
    limit=MAX_RECORDS,
):
    """Inspect existing fields in an already authorized case, without writes.

    A second participant selects the exact unordered pair, including self-pairs.
    """
    if name not in REPORT_NAMES:
        raise ValidationError("Choose a supported report.")
    if state not in {"active", "archived", "all"}:
        raise ValidationError("Choose active, archived, or all evidence.")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_RECORDS
    ):
        raise ValidationError(f"Report limits must be between 1 and {MAX_RECORDS}.")
    for value in (start, end):
        if value is not None and timezone.is_naive(value):
            raise ValidationError("Report times require an explicit timezone.")
    if start is not None and end is not None and start > end:
        raise ValidationError("The period end must not precede its start.")
    for model, identifier in (
        (Vehicle, vehicle_id),
        (Person, participant_id),
        (Person, other_participant_id),
    ):
        if identifier:
            try:
                exists = model.objects.filter(pk=identifier, case=case).exists()
            except ValidationError, ValueError:
                exists = False
            if not exists:
                raise Http404("Selection not found.")
    if other_participant_id and not participant_id:
        raise ValidationError("Choose the first participant before selecting a pair.")
    selected = REPORT_NAMES[name]
    if selected == "calls":
        if vehicle_id:
            raise ValidationError("A vehicle filter does not apply to call reports.")
        return call_report(
            case, state, participant_id, other_participant_id, start, end, limit
        )
    if selected == "vehicle":
        if participant_id or other_participant_id:
            raise ValidationError(
                "A participant filter does not apply to vehicle reports."
            )
        return vehicle_report(case, state, vehicle_id, start, end, limit)
    if vehicle_id or participant_id or other_participant_id:
        raise ValidationError(
            "Vehicle and participant filters do not apply to file-byte reports."
        )
    return duplicate_report(case, state, start, end, limit)
