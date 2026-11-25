"""Case-scoped form choices for a call history CSV and its reviewed rows."""

from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django import forms

from core.evidence.forms import validate_file
from document.models import DocumentEvidence
from people.models import Person
from phone.models import CallImportSource


class CallHistoryUploadForm(forms.Form):
    submission_id = forms.UUIDField(initial=uuid4, widget=forms.HiddenInput)
    file = forms.FileField(
        label="Call history CSV",
        required=False,
        widget=forms.ClearableFileInput(attrs={"accept": ".csv,text/csv"}),
    )
    document = forms.ModelChoiceField(
        queryset=DocumentEvidence.objects.none(),
        required=False,
        label="Retained CSV",
        empty_label="Choose a retained CSV",
    )
    source = forms.ModelChoiceField(
        queryset=CallImportSource.objects.none(),
        required=False,
        label="Source line or account",
        empty_label="Choose an existing source",
        help_text="Reuse the same source for each export from this line or account.",
    )
    source_label = forms.CharField(
        max_length=255,
        required=False,
        label="New source label",
        help_text="For a new line or account, enter a label such as Work phone.",
    )
    timezone = forms.CharField(
        max_length=64,
        label="Export timezone",
        help_text=(
            "Enter the timezone used for the Date column, such as America/Toronto."
        ),
    )
    confirm_timezone = forms.BooleanField(label="I confirm this is the export timezone")

    def __init__(self, *args, case, byte_limit, **kwargs):
        super().__init__(*args, **kwargs)
        self.byte_limit = byte_limit
        self.fields["document"].queryset = DocumentEvidence.objects.filter(
            case=case, is_archived=False, original_filename__iendswith=".csv"
        )
        self.fields["source"].queryset = CallImportSource.objects.filter(case=case)

    def clean_file(self):
        file = validate_file(self.cleaned_data["file"])
        if file and not 0 < file.size <= self.byte_limit:
            raise forms.ValidationError(
                "Choose a nonempty CSV within the displayed size limit."
            )
        return file

    def clean_document(self):
        document = self.cleaned_data["document"]
        if document and document.byte_size > self.byte_limit:
            raise forms.ValidationError(
                "Choose a retained CSV within the displayed size limit."
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
        values = super().clean()
        if "file" in values and "document" in values:
            if bool(values["file"]) == bool(values["document"]):
                self.add_error(None, "Upload one CSV or choose one retained CSV.")
        if "source" in values and "source_label" in values:
            if bool(values["source"]) == bool(values["source_label"]):
                self.add_error(
                    None, "Choose an existing source or enter one new source label."
                )
        return values


class CallHistorySelectionForm(forms.Form):
    reviewed_attempt = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, *args, case, batch, view, groups, rows, **kwargs):
        super().__init__(*args, **kwargs)
        self.view = view
        self.groups = groups
        self.rows = rows
        self.initial["reviewed_attempt"] = batch.attempt_id
        selection = batch.selection
        people = [
            (str(person.pk), str(person))
            for person in Person.objects.filter(case=case).order_by(
                "last_name", "first_name", "pk"
            )
        ]
        person_labels = dict(people)
        group_people = selection.get("group_people", {})
        row_people = selection.get("row_people", {})
        if view == "people":
            self.fields["source_person"] = forms.ChoiceField(
                label=f"Person for {batch.source_label}",
                choices=[("", "Leave source unlinked"), *people],
                required=False,
                initial=selection.get("source_person") or "",
            )
            for index, group in enumerate(groups):
                identifier = group["identifier"]
                self.fields[f"group_{index}"] = forms.ChoiceField(
                    label=f"Person for {identifier}",
                    choices=[("", "Leave participant unlinked"), *people],
                    required=False,
                    initial=group_people.get(identifier) or "",
                )
        if view == "overlaps" and rows:
            bulk_overlap = {None: "", True: "keep", False: "skip"}[
                selection.get("overlap_keep")
            ]
            self.fields["overlap_keep"] = forms.ChoiceField(
                label="Choice for all possible overlaps",
                choices=[
                    ("", "Choose how to handle overlaps"),
                    ("keep", "Keep calls separately"),
                    ("skip", "Skip possible overlaps"),
                ],
                initial=bulk_overlap,
            )
        for row in rows:
            if not row.issue and view == "rows":
                group_person = person_labels.get(group_people.get(row.identifier))
                self.fields[f"person_{row.number}"] = forms.ChoiceField(
                    label=f"Person for row {row.number}",
                    choices=[
                        (
                            "inherit",
                            f"Use group choice · {group_person or 'Unlinked'}",
                        ),
                        ("", "Leave this participant unlinked"),
                        *people,
                    ],
                    required=False,
                    initial=row_people.get(str(row.number), "inherit") or "",
                )
            if row.overlap_candidates and view in {"rows", "overlaps"}:
                overlap = selection.get("overlap_rows", {}).get(str(row.number))
                self.fields[f"overlap_{row.number}"] = forms.ChoiceField(
                    label=f"Overlap choice for row {row.number}",
                    choices=[
                        ("", "Use the choice for all overlaps"),
                        ("keep", "Keep this call separately"),
                        ("skip", "Skip this possible overlap"),
                    ],
                    required=False,
                    initial="" if overlap is None else "keep" if overlap else "skip",
                )

    def choices(self):
        data = self.cleaned_data
        parameters = {
            "group_people": {
                group["identifier"]: data[name] or None
                for index, group in enumerate(self.groups)
                if (name := f"group_{index}") in self.data
            },
            "row_people": {
                str(row.number): data[name] or None
                for row in self.rows
                if (name := f"person_{row.number}") in self.fields and name in self.data
            },
            "overlap_rows": {
                str(row.number): {"": None, "keep": True, "skip": False}[data[name]]
                for row in self.rows
                if (name := f"overlap_{row.number}") in self.fields
                and name in self.data
            },
        }
        if "source_person" in self.fields and "source_person" in self.data:
            parameters["source_person"] = data["source_person"] or None
        if "overlap_keep" in self.fields and "overlap_keep" in self.data:
            parameters["overlap_keep"] = data["overlap_keep"] == "keep"
        return parameters


class LinkCallPersonForm(forms.Form):
    role = forms.ChoiceField(label="Party to link")
    person = forms.ModelChoiceField(queryset=Person.objects.none(), label="Case person")

    def __init__(self, *args, case, item, **kwargs):
        super().__init__(*args, **kwargs)
        row = item.import_row
        source_role = "recipient" if row.direction == "incoming" else "sender"
        roles = [
            (
                role,
                f"{label} · "
                + (
                    row.batch.source_label
                    if role == source_role
                    else row.raw.get("Number") or "Unknown party"
                ),
            )
            for role, label in (("sender", "Caller"), ("recipient", "Recipient"))
            if getattr(item, f"{role}_id") is None
        ]
        self.fields["role"].choices = roles
        if len(roles) == 1:
            self.fields["role"].initial = roles[0][0]
        self.fields["person"].queryset = Person.objects.filter(case=case).order_by(
            "last_name", "first_name", "pk"
        )
