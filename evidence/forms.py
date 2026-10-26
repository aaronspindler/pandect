"""Explicit forms keep editable descriptions separate from retained originals."""

from uuid import uuid4

from django import forms
from django.conf import settings

from .models import AudioEvidence, DocumentEvidence, ImageEvidence, TextEvidence

KINDS = {
    "image": ImageEvidence,
    "audio": AudioEvidence,
    "document": DocumentEvidence,
    "text": TextEvidence,
}
KIND_LABELS = {
    "image": "Image",
    "audio": "Audio",
    "document": "Document",
    "text": "Text",
}
COMMON_FIELDS = (
    "title",
    "description",
    "source_url",
    "source_description",
    "collected_at",
)
TYPE_FIELDS = {
    "image": ("width", "height"),
    "audio": ("duration", "sample_rate", "channel_count"),
    "document": ("page_count",),
    "text": ("language",),
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
        self.fields["description"].label = "Investigator description"
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
        fields += ("body",) if kind == "text" else ("file",)
    return forms.modelform_factory(model, form=EvidenceForm, fields=fields)
