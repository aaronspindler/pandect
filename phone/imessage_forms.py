"""Explicit source time and participant choices for one CSV conversation."""

import re
from uuid import uuid4, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django import forms

from core.evidence.forms import validate_file
from document.models import DocumentEvidence
from people.models import Person
from phone.analysis.deterministic.imessage import import_file_limit


class IMessageUploadForm(forms.Form):
    submission_id = forms.UUIDField(initial=uuid4, widget=forms.HiddenInput)
    file = forms.FileField(
        required=False,
        label="iMessage CSV",
        help_text=(
            "Use the supported 18-column export, from Chat Session "
            "through Attachment type."
        ),
        widget=forms.ClearableFileInput(attrs={"accept": ".csv,text/csv"}),
    )
    document = forms.ModelChoiceField(
        queryset=DocumentEvidence.objects.none(),
        required=False,
        label="Or choose a retained document",
    )
    title = forms.CharField(required=False, max_length=255, label="New CSV title")
    source = forms.CharField(
        required=False, max_length=255, label="New CSV source name or citation"
    )
    timezone = forms.CharField(
        max_length=64,
        label="Export timezone",
        help_text=(
            "Confirm the timezone used for Message Date in this export, "
            "such as America/Toronto."
        ),
    )
    confirm_timezone = forms.BooleanField(
        label="I confirm this is the export's timezone"
    )

    def __init__(self, *args, case, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["document"].queryset = DocumentEvidence.objects.filter(case=case)

    def clean_file(self):
        original = validate_file(self.cleaned_data.get("file"))
        if original and not original.size:
            raise forms.ValidationError("Choose a nonempty CSV.")
        if original and original.size > import_file_limit():
            raise forms.ValidationError(
                "Choose a CSV within the displayed import size limit."
            )
        return original

    def clean_document(self):
        document = self.cleaned_data.get("document")
        if document and document.byte_size > import_file_limit():
            raise forms.ValidationError(
                "Choose a CSV within the displayed import size limit."
            )
        return document

    def clean_timezone(self):
        value = self.cleaned_data["timezone"]
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError, ValueError:
            raise forms.ValidationError(
                "Enter an IANA timezone, such as America/Toronto."
            ) from None
        return value

    def clean(self):
        data = super().clean()
        if bool(data.get("file")) == bool(data.get("document")):
            self.add_error(None, "Upload one CSV or choose one retained document.")
        if data.get("document") and (data.get("title") or data.get("source")):
            self.add_error(None, "Title and source details apply only to a new CSV.")
        return data


class IMessagePersonForm(forms.Form):
    mode = forms.ChoiceField(
        label="Person record",
        choices=(
            ("existing", "Use an existing case person"),
            ("create", "Create a person when importing"),
        ),
        help_text=(
            "New person details are used only when creating a record. "
            "Existing people are not edited here."
        ),
    )
    person = forms.ModelChoiceField(
        queryset=Person.objects.none(), required=False, label="Existing case person"
    )
    first_name = forms.CharField(required=False, max_length=150)
    last_name = forms.CharField(required=False, max_length=150)
    phone_number = forms.CharField(
        required=False,
        max_length=64,
        help_text="Optional when a name or email identifies the person record.",
        widget=forms.TextInput(attrs={"inputmode": "tel"}),
    )
    email = forms.EmailField(required=False, max_length=254)

    def __init__(self, *args, case, preview, role, **kwargs):
        self.preview = preview
        self.role = role
        super().__init__(*args, **kwargs)
        self.fields["person"].queryset = Person.objects.filter(case=case)
        self.fields["mode"].initial = "existing" if case.people.exists() else "create"
        if role == "correspondent":
            identifier = preview.result.data.get("imessage", {}).get("sender_id", "")
            if len(identifier) <= 64 and re.fullmatch(r"[+\d(). -]+", identifier):
                self.fields["phone_number"].initial = identifier

    def clean(self):
        data = super().clean()
        if data.get("mode") == "existing" and not data.get("person"):
            self.add_error("person", "Choose an existing person in this case.")
        if data.get("mode") == "create":
            if data.get("person"):
                self.add_error(
                    "person", "Clear the existing person to create a new record."
                )
            if not any(
                data.get(key)
                for key in ("first_name", "last_name", "phone_number", "email")
            ):
                self.add_error(
                    "first_name",
                    "Enter a name, phone number, or email for the new person.",
                )
        return data

    def specification(self):
        if self.cleaned_data["mode"] == "existing":
            return {"id": str(self.cleaned_data["person"].pk), "create": None}
        return {
            "id": str(uuid5(self.preview.pk, self.role)),
            "create": {
                key: self.cleaned_data[key]
                for key in ("first_name", "last_name", "phone_number", "email")
            },
        }


class IMessageConfirmForm(forms.Form):
    direct_chat = forms.BooleanField(
        label="I confirm this export is a direct conversation between these two people"
    )
    confirm = forms.BooleanField(
        label="Import the ready messages with these people and the confirmed timezone",
        help_text=(
            "New people are created only if the message import completes. "
            "Excluded rows stay in the retained CSV and review."
        ),
    )
