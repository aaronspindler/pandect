from django import forms
from django.core import signing
from django.core.exceptions import ValidationError

from .models import Vehicle


def vehicle_revision(vehicle):
    return signing.dumps(
        {"id": str(vehicle.pk), "updated_at": vehicle.updated_at.isoformat()},
        salt="vehicles.edit-revision",
    )


class VehicleForm(forms.ModelForm):
    revision = forms.CharField(widget=forms.HiddenInput)

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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stale = False
        if self.instance._state.adding:
            del self.fields["revision"]
        else:
            self.initial["revision"] = vehicle_revision(self.instance)

    def clean_revision(self):
        value = self.cleaned_data["revision"]
        try:
            revision = signing.loads(value, salt="vehicles.edit-revision")
        except signing.BadSignature as error:
            raise ValidationError("Reload this form before saving changes.") from error
        if not isinstance(revision, dict) or revision.get("id") != str(
            self.instance.pk
        ):
            raise ValidationError("Reload this form before saving changes.")
        if revision.get("updated_at") != self.instance.updated_at.isoformat():
            self.stale = True
        return value

    def clean(self):
        cleaned = super().clean()
        if self.stale:
            raise ValidationError(
                "This vehicle has newer saved details. Review them below before "
                "saving your proposed changes."
            )
        return cleaned


class VehicleDeleteForm(forms.Form):
    confirm = forms.BooleanField(
        label="I understand this permanently deletes this vehicle record."
    )
