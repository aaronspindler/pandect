from uuid import uuid4
from zoneinfo import available_timezones

from django import forms

from vehicles.models import Vehicle

from .vehicle_imports import parse_csv


class VehicleImportUploadForm(forms.Form):
    submission_id = forms.UUIDField(initial=uuid4, widget=forms.HiddenInput)
    file = forms.FileField(
        label="Asset Location CSV",
        widget=forms.ClearableFileInput(attrs={"accept": ".csv,text/csv"}),
    )
    timezone = forms.ChoiceField(
        label="Export timezone",
        choices=[("", "Choose the export timezone")]
        + [(name, name.replace("_", " ")) for name in sorted(available_timezones())],
        help_text=(
            "Use the timezone configured by the source export. "
            "An address does not establish its timezone."
        ),
    )
    source = forms.CharField(
        label="Source name or citation", max_length=255, required=False
    )
    use_created_time = forms.BooleanField(
        label="Use Created Time as the reported observation time",
        help_text=(
            "Received Time will be retained separately. "
            "Confirm this interpretation against your source."
        ),
    )

    def clean(self):
        cleaned = super().clean()
        original = cleaned.get("file")
        if original and cleaned.get("timezone"):
            # Only read one byte beyond the hard limit, regardless of declared size.
            content = original.read(1024 * 1024 + 1)
            original.seek(0)
            try:
                self.rows = parse_csv(content, cleaned["timezone"])
            except forms.ValidationError as error:
                self.add_error("file", error)
        return cleaned


class VehicleImportReviewForm(forms.Form):
    def __init__(self, *args, case, groups, duplicates=False, **kwargs):
        super().__init__(*args, **kwargs)
        vehicles = Vehicle.objects.filter(case=case)
        for index, (asset, serial) in enumerate(groups):
            label = asset or "Unnamed asset"
            if serial:
                label += f" · serial {serial}"
            self.fields[f"vehicle_{index}"] = forms.ModelChoiceField(
                queryset=vehicles,
                label=label,
                help_text=(
                    "Choose the case vehicle reported by this asset. "
                    "This association is not independently verified."
                ),
            )
        if duplicates:
            self.fields["confirm_duplicates"] = forms.BooleanField(
                label="Keep the possible duplicates as separate observations",
                help_text=(
                    "Matching rows already exist in this CSV or another import. "
                    "Review them above before continuing."
                ),
            )
        self.fields["confirm"] = forms.BooleanField(
            label="Import these observations with the selected vehicle associations",
            help_text=(
                "The source CSV, reported values, timezone interpretation, "
                "and associations will be retained."
            ),
        )
