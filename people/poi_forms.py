from django import forms
from django.core import signing
from django.core.exceptions import ValidationError

from .models import Person, PointOfInterest


def poi_revision(poi):
    return signing.dumps(
        {"id": str(poi.pk), "updated_at": poi.updated_at.isoformat()},
        salt="people.poi-revision",
    )


class PointOfInterestForm(forms.ModelForm):
    revision = forms.CharField(widget=forms.HiddenInput)

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
            "notes",
        )
        labels = {
            "kind": "POI type",
            "custom_label": "Custom label",
            "source": "Source / citation",
            "notes": "Notes",
        }
        widgets = {
            "latitude": forms.NumberInput(attrs={"step": "0.000001"}),
            "longitude": forms.NumberInput(attrs={"step": "0.000001"}),
            "address": forms.Textarea(attrs={"rows": 3}),
            "notes": forms.Textarea(attrs={"rows": 4}),
        }
        help_texts = {
            "custom_label": "Required for Custom, for example Gym or Meeting place.",
            "latitude": "WGS84 decimal degrees, from −90 to 90.",
            "longitude": "WGS84 decimal degrees, from −180 to 180.",
            "address": (
                "Optional context. Enter coordinates to place the pin; "
                "this address is not looked up automatically."
            ),
            "notes": (
                "Record why this place is associated with the person, "
                "including uncertainty."
            ),
        }

    def __init__(self, *args, case, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["person"].queryset = Person.objects.filter(case=case)
        self.stale = False
        if self.instance._state.adding:
            del self.fields["revision"]
        else:
            self.initial["revision"] = poi_revision(self.instance)

    def clean_revision(self):
        value = self.cleaned_data["revision"]
        try:
            revision = signing.loads(value, salt="people.poi-revision")
        except signing.BadSignature as error:
            raise ValidationError("Reload this form before saving changes.") from error
        if not isinstance(revision, dict) or revision.get("id") != str(
            self.instance.pk
        ):
            raise ValidationError("Reload this form before saving changes.")
        self.stale = revision.get("updated_at") != self.instance.updated_at.isoformat()
        return value

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("kind") == PointOfInterest.Kind.CUSTOM:
            if not cleaned.get("custom_label"):
                self.add_error("custom_label", "Enter a label for this custom POI.")
        else:
            cleaned["custom_label"] = ""
        if self.stale:
            raise ValidationError(
                "This POI has newer saved details. Review them below before "
                "saving your proposed changes."
            )
        return cleaned


class PointOfInterestDeleteForm(forms.Form):
    confirm = forms.BooleanField(
        label="I understand this permanently deletes this POI."
    )
