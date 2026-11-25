"""Case-scoped vehicle filters for deterministic location reports."""

from datetime import UTC, datetime, time, timedelta

from django import forms
from django.utils import timezone

from core.analysis.report_forms import BaseReportForm


def _vehicle_choices(case):
    return [
        ("", "All vehicles"),
        *((str(vehicle.pk), str(vehicle)) for vehicle in case.vehicles.all()),
    ]


class VehicleReportForm(BaseReportForm):
    vehicle_id = forms.ChoiceField(required=False, label="Vehicle")

    def __init__(self, *args, case, **kwargs):
        super().__init__(*args, case=case, **kwargs)
        self.fields["vehicle_id"].choices = _vehicle_choices(case)


class TripMapForm(forms.Form):
    vehicle_id = forms.ChoiceField(required=False, label="Vehicle")
    day = forms.DateField(
        required=False,
        input_formats=["%Y-%m-%d"],
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )

    def __init__(self, *args, case, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["vehicle_id"].choices = _vehicle_choices(case)
        zone_name = timezone.get_current_timezone_name()
        self.fields["day"].label = f"Jump to date ({zone_name})"
        self.day_bounds = None

    def clean_day(self):
        day = self.cleaned_data["day"]
        if day is not None:
            zone = timezone.get_current_timezone()
            try:
                next_day = day + timedelta(days=1)
                self.day_bounds = (
                    datetime.combine(day, time.min, zone).astimezone(UTC),
                    datetime.combine(next_day, time.min, zone).astimezone(UTC),
                )
            except OverflowError as error:
                raise forms.ValidationError(
                    "Choose a date with a valid start and end in this timezone."
                ) from error
        return day
