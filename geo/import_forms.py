from uuid import uuid4
from zoneinfo import available_timezones

from django import forms
from django.conf import settings

from geo.vehicle_imports import MAX_BYTES
from vehicle.models import Vehicle


class VehicleImportUploadForm(forms.Form):
    submission_id = forms.UUIDField(initial=uuid4, widget=forms.HiddenInput)
    file = forms.FileField(
        label="Asset Location CSV",
        widget=forms.ClearableFileInput(attrs={"accept": ".csv,text/csv"}),
    )
    vehicle = forms.ModelChoiceField(
        queryset=Vehicle.objects.none(),
        required=False,
        label="Vehicle",
        empty_label="Choose vehicles during review",
        help_text=(
            "Apply this case vehicle to all observations. "
            "You can change individual asset associations during review."
        ),
    )
    timezone = forms.ChoiceField(
        label="Export timezone",
        initial="America/Toronto",
        choices=[("", "Choose the export timezone")]
        + [(name, name.replace("_", " ")) for name in sorted(available_timezones())],
        help_text=(
            "Defaults to America/Toronto (Eastern time). Match the timezone in "
            "your miTrail profile preferences. "
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

    def __init__(self, *args, case, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["vehicle"].queryset = Vehicle.objects.filter(case=case)

    def clean_file(self):
        original = self.cleaned_data["file"]
        if not original.size or original.size > min(
            MAX_BYTES, settings.PANDECT_MAX_FILE_BYTES
        ):
            raise forms.ValidationError(
                "Choose a nonempty CSV within the displayed import size limit."
            )
        return original


class VehicleImportReviewForm(forms.Form):
    def __init__(
        self,
        *args,
        case,
        groups,
        duplicates=False,
        default_vehicle=None,
        vehicle_mapping=None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        choices = [("", "---------")]
        choices.extend(
            (str(vehicle.pk), str(vehicle))
            for vehicle in Vehicle.objects.filter(case=case)
        )
        vehicle_mapping = vehicle_mapping or {}
        for index, (asset, serial) in enumerate(groups):
            label = asset or "Unnamed asset"
            if serial:
                label += f" · serial {serial}"
            self.fields[f"vehicle_{index}"] = forms.ChoiceField(
                choices=choices,
                label=label,
                initial=vehicle_mapping.get(str(index), default_vehicle),
                help_text=(
                    "Choose the case vehicle reported by this asset. "
                    "This association is not independently verified."
                ),
            )
        if duplicates:
            self.fields["confirm_duplicates"] = forms.BooleanField(
                required=False,
                label="Keep the possible duplicates as separate observations",
                help_text=(
                    "Leave unchecked to skip matching rows in this CSV or another "
                    "import. The complete original CSV remains retained."
                ),
            )
        self.fields["confirm"] = forms.BooleanField(
            label="Import these observations with the selected vehicle associations",
            help_text=(
                "The source CSV, reported values, timezone interpretation, "
                "and associations will be retained."
            ),
        )
