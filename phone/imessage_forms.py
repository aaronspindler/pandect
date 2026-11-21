"""Explicit source time and participant choices for one CSV conversation."""

import re
from uuid import uuid4, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django import forms
from django.forms.models import ModelChoiceIterator
from django.utils.crypto import constant_time_compare

from core.analysis.execution import (
    execution_confirmation,
    external_ai_destination,
    inference_configured,
)
from core.evidence.forms import validate_file
from document.models import DocumentEvidence
from people.models import Person
from phone.analysis.deterministic.imessage import import_file_limit
from phone.analysis.ingest import import_analysis_executions

ANALYSIS_MODES = (
    ("local", "Local checks"),
    ("ai", "AI only"),
    ("both", "Local checks and AI"),
)
ANALYSIS_CONFIRMATION_SALT = "phone.imessage.analysis"
CREATE_PERSON = "create"


class IMessageUploadForm(forms.Form):
    submission_id = forms.UUIDField(initial=uuid4, widget=forms.HiddenInput)
    file = forms.FileField(
        required=False,
        label="iMessage CSV",
        help_text=(
            "Use the supported 18-column export, from Chat Session "
            "through Attachment type."
        ),
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
        documents = DocumentEvidence.objects.filter(case=case)
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


class IMessageAnalysisForm(forms.Form):
    mode = forms.ChoiceField(
        label="Analysis after import",
        choices=ANALYSIS_MODES,
        initial="local",
        widget=forms.RadioSelect,
        help_text=(
            "Local checks inspect identifiers, writing, and message timing. "
            "AI suggests topics, claims, summaries, and links. "
            "Each AI analysis runs once in the location shown below."
        ),
    )
    external_processing = forms.BooleanField(
        required=False,
        label="I approve external AI processing of the newly imported messages",
        help_text=(
            "Required when the selected option includes external AI. "
            "Sends selected message text and source references to the service above. "
            "Provider retention policies apply."
        ),
    )
    execution_confirmation = forms.CharField(widget=forms.HiddenInput)

    def __init__(self, *args, actor, case, run, **kwargs):
        super().__init__(*args, **kwargs)
        self.executions = import_analysis_executions()
        ai_available = all(
            inference_configured(route, key.split(":", 1)[0])
            for key, route in self.executions.items()
        )
        self.external_ai = ai_available and any(
            route["backend"] == "gateway" for route in self.executions.values()
        )
        if not ai_available:
            self.fields["mode"].choices = ANALYSIS_MODES[:1]
            self.fields[
                "mode"
            ].help_text = (
                "Local checks inspect identifiers, writing, and message timing."
            )
            self.ai_destination = (
                "AI analysis is unavailable until an operator configures a service."
            )
        elif self.external_ai:
            destinations = dict.fromkeys(
                external_ai_destination(route) for route in self.executions.values()
            )
            self.ai_destination = "AI will use " + "; ".join(destinations) + "."
        else:
            self.ai_destination = "AI will use the configured private service."
        scope = [str(actor.pk), str(case.pk), str(run.pk)]
        self.local_confirmation = execution_confirmation(
            ANALYSIS_CONFIRMATION_SALT, scope, {}
        )
        self.ai_confirmation = execution_confirmation(
            ANALYSIS_CONFIRMATION_SALT, scope, self.executions
        )
        self.confirmation = f"{self.local_confirmation}:{self.ai_confirmation}"
        if not self.is_bound:
            self.initial["execution_confirmation"] = self.confirmation

    def clean(self):
        data = super().clean()
        if (
            self.external_ai
            and data.get("mode") in {"ai", "both"}
            and not data.get("external_processing")
        ):
            self.add_error(
                "external_processing", "Approve external processing to continue."
            )
        reviewed_local, _, reviewed_ai = data.get(
            "execution_confirmation", ""
        ).partition(":")
        if not constant_time_compare(reviewed_local, self.local_confirmation) or (
            data.get("mode") != "local"
            and not constant_time_compare(reviewed_ai, self.ai_confirmation)
        ):
            self.add_error(
                None,
                "Processing settings changed or this request expired. "
                "Review the current settings and submit again.",
            )
            values = self.data.copy()
            values["execution_confirmation"] = self.confirmation
            values.pop("external_processing", None)
            self.data = values
            data["external_processing"] = False
        return data

    def specification(self):
        mode = self.cleaned_data["mode"]
        return {
            "mode": mode,
            "executions": self.executions if mode != "local" else {},
            "external_processing": bool(
                mode != "local"
                and self.external_ai
                and self.cleaned_data["external_processing"]
            ),
        }


class IMessageAnalysisRetryForm(IMessageAnalysisForm):
    submission_id = forms.UUIDField(initial=uuid4, widget=forms.HiddenInput)


class IMessageConfirmForm(IMessageAnalysisForm):
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
