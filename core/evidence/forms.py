"""Shared evidence form validation and source attribution."""

from uuid import uuid4

from django import forms
from django.conf import settings

SOURCE_FIELDS = ("source", "source_url", "source_description", "collected_at")
COMMON_FIELDS = ("title", *SOURCE_FIELDS)


class EvidenceDeleteForm(forms.Form):
    revision = forms.CharField(
        widget=forms.HiddenInput,
        error_messages={
            "required": "Review this confirmation page before deleting evidence."
        },
    )
    confirm = forms.BooleanField(
        label="I understand this permanently deletes every record and file listed here."
    )


def validate_file(original):
    if not original:
        return original
    if len(getattr(original, "content_type", "") or "") > 255:
        raise forms.ValidationError(
            "The declared content type is too long. Select the file again."
        )
    if original.size > settings.PANDECT_MAX_FILE_BYTES:
        raise forms.ValidationError("This file exceeds the configured upload limit.")
    return original


def validate_text(text):
    if len(text.encode("utf-8")) > settings.PANDECT_MAX_TEXT_BYTES:
        raise forms.ValidationError("This text exceeds the configured capture limit.")
    return text


class BaseEvidenceForm(forms.ModelForm):
    def clean_file(self):
        return validate_file(self.cleaned_data["file"])

    def clean_body(self):
        return validate_text(self.cleaned_data["body"])

    def clean_source_description(self):
        description = self.cleaned_data["source_description"]
        if len(description) > 20000:
            raise forms.ValidationError(
                "Keep this description within 20,000 characters."
            )
        return description


class EvidenceForm(BaseEvidenceForm):
    kind = forms.ChoiceField(choices=(), widget=forms.HiddenInput)
    submission_id = forms.UUIDField(
        required=False, initial=uuid4, widget=forms.HiddenInput
    )
    expected_edited_at = forms.DateTimeField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, kind, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["kind"].choices = ((kind, self.kind_label),)
        self.fields["kind"].initial = kind
        self.fields["title"].label = "Title"
        self.fields["source_url"].label = "Source URL"
        self.fields[
            "source"
        ].help_text = "Name the source, such as a witness, publication, or record."
        self.fields["collected_at"].label = "Source collected at (UTC)"
        self.fields["collected_at"].widget = forms.DateTimeInput(
            attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
        )
        units = {"width": " (pixels)", "height": " (pixels)", "sample_rate": " (Hz)"}
        for name in self.type_fields:
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
            self.fields["file"].help_text = (
                "Supported checks run automatically after upload. "
                "Results appear in Findings."
            )
        if "body" in self.fields:
            self.fields["body"].strip = False
            self.fields["body"].label = "Original text"
            self.fields["body"].help_text = (
                "We preserve the text submitted by your browser. Upload a text file "
                "as a Document when its exact original bytes matter."
            )
            if kind == "text":
                self.fields[
                    "body"
                ].help_text += (
                    " Exact duplicates in this case are checked before saving."
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
        self.configure_original_fields(kind)
        # Start with the material being captured, then its optional context.
        self.order_fields(
            ("title", *self.original_fields, "transcript", "file", *COMMON_FIELDS)
        )
        additional_names = (*SOURCE_FIELDS, *self.type_fields)
        self.additional_fields = [self[name] for name in additional_names]
        self.primary_fields = [
            field
            for field in self.visible_fields()
            if field.name not in additional_names
        ]

    def configure_original_fields(self, kind):
        """Configure the concrete app's retained input fields."""
