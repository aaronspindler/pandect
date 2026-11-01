"""Explicit forms keep editable descriptions separate from retained originals."""

from uuid import uuid4

from django import forms
from django.conf import settings

from .models import CallEvidenceAttachment
from .types import (
    CALL_KINDS,
    COMMUNICATION_FIELDS,
    COMMUNICATION_KINDS,
    FILE_KINDS,
    KIND_LABELS,
    KINDS,
    LOCATION_FIELDS,
    LOCATION_KINDS,
)

ORIGINAL_FIELDS = {
    **dict.fromkeys(FILE_KINDS, ("file",)),
    "text": ("body",),
    "location": LOCATION_FIELDS,
    "person-location": ("person", *LOCATION_FIELDS),
    "vehicle-location": ("vehicle", *LOCATION_FIELDS),
    "text-message": (*COMMUNICATION_FIELDS, "body"),
    **dict.fromkeys(CALL_KINDS, (*COMMUNICATION_FIELDS, "duration")),
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
    "vehicle-location": (),
    **dict.fromkeys(COMMUNICATION_KINDS, ()),
}


class EvidenceDeleteForm(forms.Form):
    confirm = forms.BooleanField(
        label="I understand this permanently deletes this evidence and its files."
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

    def clean(self):
        cleaned = super().clean()
        for name in ("description", "source_description"):
            if len(cleaned.get(name, "")) > 20000:
                self.add_error(name, "Keep this description within 20,000 characters.")
        return cleaned


class EvidenceForm(BaseEvidenceForm):
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
            self.fields["file"].help_text = (
                "The original is retained and cannot be replaced. "
                "Exact duplicates in this case are checked before saving."
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
        if kind in COMMUNICATION_KINDS and self.instance._state.adding:
            self.fields["submission_id"].required = True
            for name in ("sender", "recipient"):
                self.fields[name].queryset = self.fields[name].queryset.filter(
                    case_id=self.instance.case_id
                )
                self.fields[name].help_text = (
                    "Choose a person in this case. This reports an association, "
                    "not verified identity."
                )
            self.fields["occurred_at"].label = (
                "Sent at (UTC)" if kind == "text-message" else "Call started at (UTC)"
            )
            self.fields["occurred_at"].widget = forms.DateTimeInput(
                attrs={"type": "datetime-local", "step": "1"},
                format="%Y-%m-%dT%H:%M:%S",
            )
            self.fields[
                "occurred_at"
            ].help_text = "Time reported by the source; leave blank if unknown."
            if kind == "text-message":
                self.fields["body"].label = "Original message"
            else:
                if "file" in self.fields:
                    self.fields["file"].label = "Audio file"
                    self.fields["file"].help_text = (
                        "Optional original recording, retained without automatic "
                        "format verification. Later recordings are new attachments."
                    )
                    self.fields["transcript"].widget.attrs["rows"] = 12
                self.fields["duration"].help_text = (
                    "Reported duration, such as 00:01:30. "
                    "Leave blank if unknown; zero is valid."
                )
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
            if "vehicle" in self.fields:
                self.fields["vehicle"].queryset = self.fields[
                    "vehicle"
                ].queryset.filter(case_id=self.instance.case_id)
                self.fields["vehicle"].help_text = (
                    "Choose a vehicle in this case. This reports an association, "
                    "not verified identity, ownership, or presence."
                )
        # Start with the material being captured, then its optional context.
        self.order_fields(
            ("title", *ORIGINAL_FIELDS[kind], "transcript", "file", *COMMON_FIELDS)
        )


class CallContentForm(forms.Form):
    transcript = forms.CharField(
        required=False,
        strip=False,
        widget=forms.Textarea(attrs={"rows": 12, "spellcheck": "false"}),
        help_text=(
            "Supplied transcript, retained as entered. "
            "Later corrections are additional attachments."
        ),
    )
    file = forms.FileField(
        required=False,
        label="Audio file",
        help_text=(
            "Optional original recording. Its format is declared, "
            "not automatically verified."
        ),
    )

    def clean_transcript(self):
        return validate_text(self.cleaned_data["transcript"])

    def clean_file(self):
        return validate_file(self.cleaned_data["file"])


class CallCreationForm(EvidenceForm, CallContentForm):
    pass


class CallAttachmentForm(CallContentForm):
    submission_id = forms.UUIDField(initial=uuid4, widget=forms.HiddenInput)

    def clean(self):
        cleaned = super().clean()
        if (
            not self.errors
            and not cleaned.get("transcript")
            and not cleaned.get("file")
        ):
            raise forms.ValidationError("Supply a transcript, an audio file, or both.")
        return cleaned


def save_call_content(call, actor, data, submission_id):
    if not data.get("transcript") and not data.get("file"):
        return None
    return CallEvidenceAttachment.objects.create(
        case_id=call.case_id,
        uploaded_by=actor,
        submission_id=submission_id,
        **{
            "phone_call"
            if call.communication_kind == "phone-call"
            else "facetime": call
        },
        transcript=data.get("transcript", ""),
        file=data.get("file") or "",
    )


def evidence_form(kind, *, editing=False):
    model = KINDS[kind]
    fields = COMMON_FIELDS + TYPE_FIELDS[kind]
    if not editing:
        fields += ORIGINAL_FIELDS[kind]
    base = CallCreationForm if kind in CALL_KINDS and not editing else EvidenceForm
    return forms.modelform_factory(model, form=base, fields=fields)
