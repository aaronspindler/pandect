"""Domain-owned analysis assumptions and parameter validation."""

from django import forms

from core.analysis.form_base import BaseAnalysisForm, selection
from core.analysis.sources import source_id
from people.models import Person


class PhoneAnalysisForm(BaseAnalysisForm):
    compare_split_at = forms.DateTimeField(
        required=False,
        label="Compare communications before and after (UTC)",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )

    me_label = forms.CharField(
        required=False, max_length=255, initial="Me", label="First source account label"
    )

    them_label = forms.CharField(
        required=False,
        max_length=255,
        initial="Them",
        label="Second source account label",
    )

    break_before = forms.MultipleChoiceField(
        required=False,
        label="Start an inferred session before",
        widget=forms.SelectMultiple(attrs={"size": 6}),
    )

    join_before = forms.MultipleChoiceField(
        required=False,
        label="Continue an inferred session before",
        widget=forms.SelectMultiple(attrs={"size": 6}),
    )

    semantic_links = forms.BooleanField(
        required=False,
        label="Suggest topic continuation across channels",
        help_text="Uses the selected processing location and requires review.",
    )

    gap_minutes = forms.FloatField(
        required=False,
        min_value=1,
        max_value=10080,
        label="Observation/session gap (minutes)",
        initial=60,
    )

    time_window_minutes = forms.FloatField(
        required=False,
        min_value=0,
        max_value=1440,
        label="Comparison time window (minutes)",
        initial=10,
    )

    timezone = forms.CharField(
        required=False,
        initial="UTC",
        label="Export timezone",
        help_text="An explicit IANA timezone, such as America/Toronto.",
    )

    me = forms.ModelChoiceField(
        queryset=Person.objects.none(),
        required=False,
        label="Export account labelled Me",
    )

    them = forms.ModelChoiceField(
        queryset=Person.objects.none(),
        required=False,
        label="Export account labelled Them",
    )

    operation_fields = {
        "sessions": ("gap_minutes", "break_before", "join_before"),
        "session_summaries": ("gap_minutes", "break_before", "join_before"),
        "comm_patterns": (
            "gap_minutes",
            "break_before",
            "join_before",
            "compare_split_at",
        ),
        "conversation_link": ("time_window_minutes", "semantic_links"),
        "conversation_import": ("me_label", "them_label", "me", "them", "timezone"),
    }
    field_order = [
        "compare_split_at",
        "me_label",
        "them_label",
        "break_before",
        "join_before",
        "semantic_links",
        "backend",
        "operation",
        "submission_id",
        "sources",
        "external_processing",
        "gap_minutes",
        "time_window_minutes",
        "timezone",
        "me",
        "them",
    ]

    def __init__(self, *args, case, **kwargs):
        super().__init__(*args, case=case, **kwargs)
        for name in ("break_before", "join_before"):
            if name in self.fields:
                self.fields[name].choices = self.fields["sources"].choices
        if self.operation_name == "conversation_import":
            people = Person.objects.filter(case=case)
            self.fields["me"].queryset = people
            self.fields["them"].queryset = people
            # Mapping has its own visible fieldset, separate from assumptions.
            self.assumption_names = ()

    def parameter_values(self):
        parameters = {
            key: self.cleaned_data[key]
            for key in (
                "gap_minutes",
                "time_window_minutes",
                "timezone",
                "semantic_links",
            )
            if self.cleaned_data.get(key) not in (None, "")
        }
        if self.cleaned_data.get("compare_split_at"):
            parameters["compare_split_at"] = self.cleaned_data[
                "compare_split_at"
            ].isoformat()
        if self.cleaned_data.get("me") and self.cleaned_data.get("them"):
            me, them = (
                str(self.cleaned_data["me"].pk),
                str(self.cleaned_data["them"].pk),
            )
            first = self.cleaned_data.get("me_label") or "Me"
            second = self.cleaned_data.get("them_label") or "Them"
            parameters |= {
                "sender_map": {first: me, second: them},
                "recipient_map": {first: them, second: me},
            }
        for key in ("break_before", "join_before"):
            if self.cleaned_data.get(key):
                parameters[key] = [
                    source_id(selection(value)) for value in self.cleaned_data[key]
                ]
        return parameters

    def clean(self):
        data = super().clean()
        if data.get("me_label") and data.get("me_label") == data.get("them_label"):
            self.add_error("them_label", "Use different source account labels.")
        chosen = set(data.get("sources", []))
        for key in ("break_before", "join_before"):
            if set(data.get(key, [])) - chosen:
                self.add_error(key, "Choose sources included in this analysis.")
        return data
