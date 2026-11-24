"""Explicit source time and participant choices for one CSV conversation."""

import re
from uuid import uuid4, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django import forms
from django.forms.models import ModelChoiceIterator

from core.evidence.forms import validate_file
from document.models import DocumentEvidence
from people.models import Person
from phone.analysis.deterministic.imessage import import_file_limit

CREATE_PERSON = "create"


class IMessageUploadForm(forms.Form):
    submission_id = forms.UUIDField(initial=uuid4, widget=forms.HiddenInput)
    file = forms.FileField(
        required=False,
        label="Message CSV",
        help_text="Use a supported iMessage or WhatsApp CSV export.",
        error_messages={"required": "Choose a CSV."},
        widget=forms.ClearableFileInput(attrs={"accept": ".csv,text/csv"}),
    )
    document = forms.ModelChoiceField(
        queryset=DocumentEvidence.objects.none(),
        required=False,
        label="Retained CSV",
        empty_label="Choose a retained document",
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
    start_date = forms.DateField(
        required=False,
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    end_date = forms.DateField(
        required=False,
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )

    def __init__(self, *args, case, **kwargs):
        super().__init__(*args, **kwargs)
        documents = DocumentEvidence.objects.filter(case=case, is_archived=False)
        self.fields["document"].queryset = documents
        self.has_retained_documents = documents.exists()
        self.fields["file"].required = not self.has_retained_documents

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
        if (
            not self.has_error("file")
            and not self.has_error("document")
            and bool(data.get("file")) == bool(data.get("document"))
        ):
            self.add_error(None, "Upload one CSV or choose one retained document.")
        if data.get("document") and (data.get("title") or data.get("source")):
            self.add_error(None, "Title and source details apply only to a new CSV.")
        if (
            data.get("start_date")
            and data.get("end_date")
            and data["end_date"] < data["start_date"]
        ):
            self.add_error(
                "end_date", "The end date must be on or after the start date."
            )
        return data


class _PersonChoices(ModelChoiceIterator):
    def __iter__(self):
        yield from super().__iter__()
        yield CREATE_PERSON, "Create a person"

    def __len__(self):
        return super().__len__() + 1


class _PersonChoiceField(forms.ModelChoiceField):
    iterator = _PersonChoices

    def to_python(self, value):
        if value == CREATE_PERSON:
            return value
        return super().to_python(value)


class IMessagePersonForm(forms.Form):
    detail_fields = ("first_name", "last_name", "phone_number", "email")

    person = _PersonChoiceField(
        queryset=Person.objects.none(),
        label="Person",
        empty_label="Choose a person",
        widget=forms.Select(attrs={"data-imessage-create": CREATE_PERSON}),
    )
    first_name = forms.CharField(required=False, max_length=150)
    last_name = forms.CharField(required=False, max_length=150)
    phone_number = forms.CharField(
        required=False,
        max_length=64,
        help_text="Optional when a name or email identifies the person record.",
        widget=forms.TextInput(attrs={"inputmode": "tel"}),
    )
    # A draft email must not block native submission when picking an existing person.
    email = forms.EmailField(
        required=False,
        max_length=254,
        widget=forms.TextInput(attrs={"inputmode": "email"}),
    )

    def __init__(self, *args, case, preview, role, **kwargs):
        self.preview = preview
        self.role = role
        super().__init__(*args, **kwargs)
        self.fields["person"].queryset = Person.objects.filter(case=case)
        if self.is_bound and not self.creating_person:
            self.data = self.data.copy()
            for name in self.detail_fields:
                self.data.pop(self.add_prefix(name), None)
        if not self.is_bound and not self.fields["person"].queryset.exists():
            self.fields["person"].initial = CREATE_PERSON
        if role == "correspondent":
            identifier = preview.result.data.get("imessage", {}).get("sender_id", "")
            if len(identifier) <= 64 and re.fullmatch(r"[+\d(). -]+", identifier):
                self.fields["phone_number"].initial = identifier

    def clean(self):
        data = super().clean()
        if data.get("person") == CREATE_PERSON and not any(
            data.get(key) for key in self.detail_fields
        ):
            self.add_error(
                "first_name", "Enter a name, phone number, or email for the new person."
            )
        return data

    @property
    def creating_person(self):
        return self["person"].value() == CREATE_PERSON

    def specification(self):
        person = self.cleaned_data["person"]
        if person != CREATE_PERSON:
            return {"id": str(person.pk), "create": None}
        return {
            "id": str(uuid5(self.preview.pk, self.role)),
            "create": {key: self.cleaned_data[key] for key in self.detail_fields},
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
