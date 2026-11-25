"""Domain-owned analysis assumptions and parameter validation."""

from uuid import uuid4

from django import forms

from core.analysis.form_base import BaseAnalysisForm
from core.analysis.sources import selection, source_id


class PhoneAnalysisForm(BaseAnalysisForm):
    compare_split_at = forms.DateTimeField(
        required=False,
        label="Compare communications before and after (UTC)",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
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

    operation_fields = {
        "sessions": ("gap_minutes", "break_before", "join_before"),
        "session_summaries": ("gap_minutes", "break_before", "join_before"),
        "comm_patterns": (
            "gap_minutes",
            "break_before",
            "join_before",
            "compare_split_at",
        ),
        "conversation_link": ("time_window_minutes",),
    }
    field_order = [
        "compare_split_at",
        "break_before",
        "join_before",
        "operation",
        "submission_id",
        "sources",
        "gap_minutes",
        "time_window_minutes",
    ]

    def __init__(self, *args, case, **kwargs):
        super().__init__(*args, case=case, **kwargs)
        for name in ("break_before", "join_before"):
            if name in self.fields:
                self.fields[name].choices = self.fields["sources"].choices

    def parameter_values(self):
        parameters = {
            key: self.cleaned_data[key]
            for key in (
                "gap_minutes",
                "time_window_minutes",
            )
            if self.cleaned_data.get(key) not in (None, "")
        }
        if self.cleaned_data.get("compare_split_at"):
            parameters["compare_split_at"] = self.cleaned_data[
                "compare_split_at"
            ].isoformat()
        for key in ("break_before", "join_before"):
            if self.cleaned_data.get(key):
                parameters[key] = [
                    source_id(selection(value)) for value in self.cleaned_data[key]
                ]
        return parameters

    def clean(self):
        data = super().clean()
        chosen = set(data.get("sources", []))
        for key in ("break_before", "join_before"):
            if set(data.get(key, [])) - chosen:
                self.add_error(key, "Choose sources included in this analysis.")
        return data


class ConversationGroupingForm(forms.Form):
    submission_id = forms.UUIDField(initial=uuid4, widget=forms.HiddenInput)
    reviewed_scope = forms.CharField(widget=forms.HiddenInput)
    gap_minutes = forms.FloatField(
        min_value=1,
        max_value=10080,
        label="Minutes between groups",
        initial=60,
        help_text=(
            "A longer pause usually starts a new group. "
            "Clear replies within four hours can continue it. "
            "A farewell followed by a greeting can start a new group."
        ),
    )
