from django import forms

from core.evidence.forms import EvidenceForm


class LocationEvidenceFormBase(EvidenceForm):
    def configure_original_fields(self, kind):
        if self.instance._state.adding:
            self.fields["submission_id"].required = True
            self.fields["address"].strip = False
            self.fields["address"].help_text = (
                "Enter an address or both coordinates. "
                "These original values are retained."
            )
            self.fields[
                "latitude"
            ].help_text = "Decimal degrees, from -90 to 90 (WGS 84)."
            self.fields[
                "longitude"
            ].help_text = "Decimal degrees, from -180 to 180 (WGS 84)."
            self.fields["observed_at"].label = "Observed at (UTC)"
            self.fields[
                "observed_at"
            ].help_text = (
                "When the source reports this location; leave blank if unknown."
            )
            self.fields["observed_at"].widget = forms.DateTimeInput(
                attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            )
            if "person" in self.fields:
                self.fields["person"].queryset = self.fields["person"].queryset.filter(
                    case_id=self.instance.case_id
                )
                self.fields["person"].help_text = (
                    "Choose a person in this case. This reports an association, "
                    "not verified identity or presence."
                )
            if "vehicle" in self.fields:
                self.fields["vehicle"].queryset = self.fields[
                    "vehicle"
                ].queryset.filter(case_id=self.instance.case_id)
                self.fields["vehicle"].help_text = (
                    "Choose a vehicle in this case. This reports an association, "
                    "not verified identity, ownership, or presence."
                )


class LocationEvidenceForm(LocationEvidenceFormBase):
    kind_label = "Location"
    original_fields = ("address", "latitude", "longitude", "observed_at")
    type_fields = ()


class PersonLocationEvidenceForm(LocationEvidenceFormBase):
    kind_label = "Person location"
    original_fields = ("person", "address", "latitude", "longitude", "observed_at")
    type_fields = ()


class VehicleLocationEvidenceForm(LocationEvidenceFormBase):
    kind_label = "Vehicle location"
    original_fields = ("vehicle", "address", "latitude", "longitude", "observed_at")
    type_fields = ()
