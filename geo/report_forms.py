"""Case-scoped vehicle filters for deterministic location reports."""

from django import forms

from core.analysis.report_forms import BaseReportForm


class VehicleReportForm(BaseReportForm):
    vehicle_id = forms.ChoiceField(required=False, label="Vehicle")

    def __init__(self, *args, case, **kwargs):
        super().__init__(*args, case=case, **kwargs)
        self.fields["vehicle_id"].choices = [
            ("", "All vehicles"),
            *((str(vehicle.pk), str(vehicle)) for vehicle in case.vehicles.all()),
        ]
