"""Domain-owned analysis assumptions and parameter validation."""

from django import forms

from core.analysis.form_base import BaseAnalysisForm, selection
from core.analysis.sources import source_id


class TextAnalysisForm(BaseAnalysisForm):
    sample_origin = forms.ChoiceField(
        required=False,
        choices=(
            ("original", "Original writing"),
            ("translated", "Translated"),
            ("edited", "Edited"),
            ("dictated", "Dictated"),
            ("ocr", "OCR"),
            ("transcription", "Transcribed"),
        ),
        initial="original",
        label="Writing sample context",
    )

    sample_language = forms.CharField(
        required=False,
        max_length=100,
        label="Reported language or languages",
        help_text="Source context, not automatic language identification.",
    )

    sample_channel = forms.CharField(
        required=False, max_length=100, label="Writing channel"
    )

    sample_topic = forms.CharField(
        required=False, max_length=200, label="Writing topic"
    )

    comparison_channel = forms.CharField(
        required=False, max_length=100, label="Second sample channel"
    )

    comparison_topic = forms.CharField(
        required=False, max_length=200, label="Second sample topic"
    )

    group_a = forms.MultipleChoiceField(
        required=False,
        label="First writing sample",
        widget=forms.SelectMultiple(attrs={"size": 8}),
    )

    group_b = forms.MultipleChoiceField(
        required=False,
        label="Second writing sample",
        widget=forms.SelectMultiple(attrs={"size": 8}),
    )

    exclude_phrases = forms.CharField(
        required=False,
        max_length=8000,
        widget=forms.Textarea,
        label="Quoted or copied wording to exclude",
        help_text=(
            "One exact phrase per line. All matching occurrences in the "
            "selected original text are excluded from writing measurements."
        ),
    )

    operation_fields = {
        "style_change": (
            "group_a",
            "group_b",
            "exclude_phrases",
            "sample_origin",
            "sample_language",
            "sample_channel",
            "sample_topic",
            "comparison_channel",
            "comparison_topic",
        ),
        "style_profiles": (
            "exclude_phrases",
            "sample_origin",
            "sample_language",
            "sample_channel",
            "sample_topic",
        ),
    }
    task_fields_by_operation = {"style_change": ("group_a", "group_b")}
    field_order = [
        "sample_origin",
        "sample_language",
        "sample_channel",
        "sample_topic",
        "comparison_channel",
        "comparison_topic",
        "group_a",
        "group_b",
        "exclude_phrases",
        "operation",
        "submission_id",
        "sources",
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("group_a", "group_b"):
            if name in self.fields:
                self.fields[name].choices = self.fields["sources"].choices
                self.fields[name].required = True

    def parameter_values(self):
        parameters = {
            key: self.cleaned_data[key]
            for key in ("exclude_phrases",)
            if self.cleaned_data.get(key) not in (None, "")
        }
        for key in ("group_a", "group_b"):
            if self.cleaned_data.get(key):
                parameters[key] = [
                    source_id(selection(value)) for value in self.cleaned_data[key]
                ]
        if self.cleaned_data.get("operation") in {"style_profiles", "style_change"}:
            contexts = {}
            for choice in self.cleaned_data.get("sources", []):
                key = source_id(selection(choice))
                second = key in parameters.get("group_b", [])
                contexts[key] = {
                    "origin": self.cleaned_data.get("sample_origin") or "original",
                    "language": self.cleaned_data.get("sample_language", ""),
                    "channel": self.cleaned_data.get(
                        "comparison_channel" if second else "sample_channel", ""
                    ),
                    "topic": self.cleaned_data.get(
                        "comparison_topic" if second else "sample_topic", ""
                    ),
                }
            parameters["contexts"] = contexts
        return parameters

    def clean(self):
        data = super().clean()
        chosen = set(data.get("sources", []))
        for key in ("group_a", "group_b"):
            if set(data.get(key, [])) - chosen:
                self.add_error(key, "Choose sources included in this analysis.")
        if data.get("operation") == "style_change" and (
            not data.get("group_a")
            or not data.get("group_b")
            or set(data.get("group_a", [])) & set(data.get("group_b", []))
        ):
            self.add_error("group_b", "Choose two nonempty, separate writing samples.")
        return data
