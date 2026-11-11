from django import forms

from geo.models import PointOfInterest
from people.models import Person
from ui.forms import SignedRevisionForm


class PointOfInterestForm(SignedRevisionForm):
    revision_salt = "people.poi-revision"
    stale_message = (
        "This POI has newer saved details. Review them below before "
        "saving your proposed changes."
    )

    class Meta:
        model = PointOfInterest
        fields = (
            "person",
            "kind",
            "custom_label",
            "latitude",
            "longitude",
            "address",
            "source",
        )
        labels = {
            "kind": "POI type",
            "custom_label": "Custom label",
            "source": "Source / citation",
        }
        widgets = {
            "latitude": forms.NumberInput(attrs={"step": "0.000001"}),
            "longitude": forms.NumberInput(attrs={"step": "0.000001"}),
            "address": forms.Textarea(attrs={"rows": 3}),
        }
        help_texts = {
            "custom_label": "Required for Custom, for example Gym or Meeting place.",
            "latitude": "WGS84 decimal degrees, from −90 to 90.",
            "longitude": "WGS84 decimal degrees, from −180 to 180.",
            "address": (
                "Optional context. Enter coordinates to place the pin; "
                "this address is not looked up automatically."
            ),
        }

    def __init__(self, *args, case, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["person"].queryset = Person.objects.filter(case=case)

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("kind") == PointOfInterest.Kind.CUSTOM:
            if not cleaned.get("custom_label"):
                self.add_error("custom_label", "Enter a label for this custom POI.")
        else:
            cleaned["custom_label"] = ""
        return cleaned


class PointOfInterestDeleteForm(forms.Form):
    confirm = forms.BooleanField(
        label="I understand this permanently deletes this POI."
    )
