"""Explicit forms keep editable descriptions separate from retained originals."""

from uuid import uuid4

from django import forms
from django.conf import settings

from .models import (
    AudioEvidence,
    DocumentEvidence,
    ImageEvidence,
    LocationEvidence,
    PersonLocationEvidence,
    TextEvidence,
)

KINDS = {
    "image": ImageEvidence,
    "audio": AudioEvidence,
    "document": DocumentEvidence,
    "text": TextEvidence,
    "location": LocationEvidence,
    "person-location": PersonLocationEvidence,
}
KIND_LABELS = {
    "image": "Image",
    "audio": "Audio",
    "document": "Document",
    "text": "Text",
    "location": "Location",
    "person-location": "Person location",
}
FILE_KINDS = ("image", "audio", "document")
LOCATION_KINDS = ("location", "person-location")
LOCATION_FIELDS = ("address", "latitude", "longitude", "observed_at")
ORIGINAL_FIELDS = {
    **dict.fromkeys(FILE_KINDS, ("file",)),
    "text": ("body",),
    "location": LOCATION_FIELDS,
    "person-location": ("person", *LOCATION_FIELDS),
}
COMMON_FIELDS = (
    "title",
    "description",
    "source",
    "source_url",
    "source_description",
    "collected_at",
)
TYPE_FIELDS = {
    "image": ("width", "height"),
    "audio": ("duration", "sample_rate", "channel_count"),
    "document": ("page_count",),
    "text": ("language",),
    "location": (),
    "person-location": (),
}


class EvidenceForm(forms.ModelForm):
    kind = forms.ChoiceField(
        choices=tuple(KIND_LABELS.items()), widget=forms.HiddenInput
    )
    submission_id = forms.UUIDField(
        required=False, initial=uuid4, widget=forms.HiddenInput
    )
    expected_edited_at = forms.DateTimeField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, kind, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["kind"].initial = kind
        self.fields["title"].label = "Title"
        self.fields["source_url"].label = "Source URL"
        self.fields["description"].label = "Investigator description"
        self.fields[
            "source"
        ].help_text = "Name the source, such as a witness, publication, or record."
        self.fields[
            "description"
        ].help_text = "Your description does not change the original."
        self.fields["collected_at"].label = "Source collected at (UTC)"
        self.fields["collected_at"].widget = forms.DateTimeInput(
            attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
        )
        units = {"width": " (pixels)", "height": " (pixels)", "sample_rate": " (Hz)"}
        for name in TYPE_FIELDS[kind]:
            field = self.fields[name]
            field.label = f"Declared {field.label.lower()}{units.get(name, '')}"
            field.help_text = "Information you supply; not automatically verified."
            if name == "duration":
                field.help_text += " Use a duration such as 00:01:30."
            elif name == "language":
                field.help_text += " Use a language tag such as en."
        for field in self.fields.values():
            if isinstance(field.widget, forms.Textarea):
                field.widget.attrs["rows"] = 4
        if self.instance and not self.instance._state.adding:
            self.fields["expected_edited_at"].required = True
            self.fields[
                "expected_edited_at"
            ].initial = self.instance.edited_at.isoformat()
            self.fields["submission_id"].initial = self.instance.submission_id
        elif "file" in self.fields:
            self.fields["submission_id"].required = True
        if "file" in self.fields:
            self.fields["file"].label = "Original file"
            self.fields[
                "file"
            ].help_text = "The original is retained and cannot be replaced."
        if "body" in self.fields:
            self.fields["body"].strip = False
            self.fields["body"].label = "Original text"
            self.fields["body"].help_text = (
                "We preserve the text submitted by your browser. Upload a text file "
                "as a Document when its exact original bytes matter."
            )
            self.fields["body"].widget.attrs.update(
                {
                    "wrap": "soft",
                    "spellcheck": "false",
                    "autocorrect": "off",
                    "autocapitalize": "off",
                    "rows": 12,
                }
            )
            self.fields["submission_id"].required = True
        if kind in LOCATION_KINDS and self.instance._state.adding:
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

    def clean_file(self):
        original = self.cleaned_data["file"]
        if len(getattr(original, "content_type", "") or "") > 255:
            raise forms.ValidationError(
                "The declared content type is too long. Select the file again."
            )
        if original.size > getattr(
            settings, "PANDECT_MAX_FILE_BYTES", 100 * 1024 * 1024
        ):
            raise forms.ValidationError(
                "This file exceeds the configured upload limit."
            )
        return original

    def clean_body(self):
        body = self.cleaned_data["body"]
        if len(body.encode("utf-8")) > getattr(
            settings, "PANDECT_MAX_TEXT_BYTES", 1024 * 1024
        ):
            raise forms.ValidationError(
                "This text exceeds the configured capture limit."
            )
        return body

    def clean(self):
        cleaned = super().clean()
        for name in ("description", "source_description"):
            if len(cleaned.get(name, "")) > 20000:
                self.add_error(name, "Keep this description within 20,000 characters.")
        return cleaned


def evidence_form(kind, *, editing=False):
    model = KINDS[kind]
    fields = COMMON_FIELDS + TYPE_FIELDS[kind]
    if not editing:
        fields += ORIGINAL_FIELDS[kind]
    return forms.modelform_factory(model, form=EvidenceForm, fields=fields)
