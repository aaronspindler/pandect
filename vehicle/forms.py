from django import forms

from ui.forms import SignedRevisionForm
from vehicle.models import Vehicle


class VehicleForm(SignedRevisionForm):
    revision_salt = "vehicle.edit-revision"
    stale_message = (
        "This vehicle has newer saved details. Review them below before "
        "saving your proposed changes."
    )

    class Meta:
        model = Vehicle
        fields = (
            "make",
            "model",
            "year",
            "color",
            "license_plate",
            "vin",
        )
        widgets = {
            "make": forms.TextInput(attrs={"autocomplete": "off"}),
            "model": forms.TextInput(attrs={"autocomplete": "off"}),
            "license_plate": forms.TextInput(attrs={"autocomplete": "off"}),
            "vin": forms.TextInput(attrs={"autocomplete": "off"}),
        }
        help_texts = {
            "year": "Leave blank if unknown.",
            "license_plate": "Preserve the reported formatting.",
            "vin": (
                "Partial or historical identifiers are allowed; "
                "no VIN verification is performed."
            ),
        }


class VehicleDeleteForm(forms.Form):
    confirm = forms.BooleanField(
        label="I understand this permanently deletes this vehicle record."
    )
