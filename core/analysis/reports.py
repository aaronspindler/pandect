"""Case-scoped validation and explicit dispatch for current-field reports."""

from django.core.exceptions import ValidationError
from django.http import Http404
from django.utils import timezone

from core.analysis.deterministic.reports import duplicate_report
from core.analysis.reporting import PAGE_SIZE
from geo.analysis.deterministic.reports import vehicle_report
from geo.analysis.deterministic.trips import vehicle_trip_report
from people.models import Person
from phone.analysis.deterministic.reports import call_report
from vehicle.models import Vehicle

REPORT_NAMES = {
    "calls": "calls",
    "vehicle-coverage": "vehicle",
    "vehicle-speed": "vehicle",
    "vehicle-trips": "vehicle",
    "duplicates": "duplicates",
}


def report(
    case,
    name,
    *,
    vehicle_id=None,
    participant_id=None,
    other_participant_id=None,
    start=None,
    end=None,
    page_size=PAGE_SIZE,
    page=1,
    gap_page=1,
    pair_page=1,
    duplicate_row=None,
):
    """Inspect existing fields in an already authorized case, without writes.

    A second participant selects the exact unordered pair, including self-pairs.
    """
    if name not in REPORT_NAMES:
        raise ValidationError("Choose a supported report.")
    if (
        isinstance(page_size, bool)
        or not isinstance(page_size, int)
        or not 1 <= page_size <= PAGE_SIZE
    ):
        raise ValidationError(f"Report page sizes must be between 1 and {PAGE_SIZE}.")
    if duplicate_row and name not in {"vehicle-coverage", "vehicle-speed"}:
        raise ValidationError("Matching CSV rows apply only to observation reports.")
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
            case,
            participant_id,
            other_participant_id,
            start,
            end,
            page,
            page_size,
            pair_page,
        )
    if selected == "vehicle":
        if participant_id or other_participant_id:
            raise ValidationError(
                "A participant filter does not apply to vehicle reports."
            )
        if name == "vehicle-trips":
            return vehicle_trip_report(
                case, vehicle_id, start, end, page_size, page, gap_page
            )
        return vehicle_report(
            case, vehicle_id, start, end, page, page_size, duplicate_row
        )
    if vehicle_id or participant_id or other_participant_id:
        raise ValidationError(
            "Vehicle and participant filters do not apply to file-byte reports."
        )
    return duplicate_report(case, start, end, page, page_size)
