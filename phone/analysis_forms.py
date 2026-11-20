"""Domain-owned analysis assumptions and parameter validation."""

from uuid import uuid4

from django import forms

from core.analysis.approval import prepare_external_approval, validate_external_approval
from core.analysis.form_base import BaseAnalysisForm, selection
from core.analysis.sources import source_id


class InstanceSummaryForm(forms.Form):
    submission_id = forms.UUIDField(initial=uuid4, widget=forms.HiddenInput)
    backend = forms.ChoiceField(
        label="Processing location",
        choices=BaseAnalysisForm.base_fields["backend"].choices,
        initial="local",
    )
    external_processing = forms.BooleanField(
        required=False,
        label="I approve sending these messages to the external AI service",
        help_text=(
            "External processing sends the selected messages through the operator’s "
            "Cloudflare AI Gateway. Previously sent requests cannot be recalled. "
            "Operator retention policies apply."
        ),
    )

    def __init__(self, *args, case, actor, instance_id, **kwargs):
        super().__init__(*args, **kwargs)
        prepare_external_approval(
            self,
            actor=actor,
            case=case,
            operation="session_summaries",
            scope=(str(instance_id),),
        )

    def clean(self):
        data = super().clean()
        if data.get("backend") != "gateway":
            data["external_processing"] = False
        elif not data.get("external_processing"):
            self.add_error(
                "external_processing", "Approve external processing for these messages."
            )
        else:
            validate_external_approval(self, data)
        return data


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
    }
    field_order = [
        "compare_split_at",
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
                "semantic_links",
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
