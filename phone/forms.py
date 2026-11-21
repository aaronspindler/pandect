from uuid import uuid4

from django import forms

from core.evidence.forms import EvidenceForm, validate_file, validate_text
from phone.models import CallEvidenceAttachment


class ConversationDayForm(forms.Form):
    day = forms.DateField(
        input_formats=["%Y-%m-%d"],
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )


class CommunicationEvidenceForm(EvidenceForm):
    def configure_original_fields(self, kind):
        if self.instance._state.adding:
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


class TextMessageEvidenceForm(CommunicationEvidenceForm):
    kind_label = "Text message"
    original_fields = ("sender", "recipient", "occurred_at", "body")
    type_fields = ()


class PhoneCallEvidenceForm(CommunicationEvidenceForm, CallContentForm):
    kind_label = "Phone call"
    original_fields = ("sender", "recipient", "occurred_at", "duration")
    type_fields = ()


class FaceTimeEvidenceForm(CommunicationEvidenceForm, CallContentForm):
    kind_label = "FaceTime"
    original_fields = ("sender", "recipient", "occurred_at", "duration")
    type_fields = ()
