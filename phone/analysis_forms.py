"""Domain-owned analysis assumptions and parameter validation."""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django import forms

from core.analysis.form_base import BaseAnalysisForm, selection
from core.analysis.sources import source_id
from phone.import_mapping import MappingFormSet, edit_rows


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

    timezone = forms.CharField(
        required=False,
        initial="UTC",
        label="Export timezone",
        help_text="An explicit IANA timezone, such as America/Toronto.",
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
        "conversation_import": ("timezone",),
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
        "timezone",
    ]

    def __init__(self, *args, case, **kwargs):
        data = args[0] if args else kwargs.get("data")
        mapping = kwargs.get("operation") == "conversation_import"
        self.is_mapping_edit = mapping and bool(data and "mapping_action" in data)
        rows = [{"source_label": "Me"}, {"source_label": "Them"}]
        self.mapping_message = ""
        if self.is_mapping_edit:
            rows, self.mapping_message = edit_rows(data)
            initial = dict(kwargs.get("initial") or {})
            for name, field in self.base_fields.items():
                if name in data:
                    initial[name] = (
                        data.getlist(name)
                        if isinstance(field, forms.MultipleChoiceField)
                        and hasattr(data, "getlist")
                        else data[name]
                    )
            kwargs["initial"] = initial
            if args:
                args = (None, *args[1:])
            else:
                kwargs["data"] = None
        super().__init__(*args, case=case, **kwargs)
        for name in ("break_before", "join_before"):
            if name in self.fields:
                self.fields[name].choices = self.fields["sources"].choices
        if self.operation_name == "conversation_import":
            self.fields["timezone"].required = True
            self.mapping_formset = MappingFormSet(
                data if not self.is_mapping_edit else None,
                prefix="participants",
                initial=rows,
                form_kwargs={"case": case},
            )
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
        if self.operation_name == "conversation_import":
            parameters |= {"sender_map": {}, "recipient_map": {}}
            for row in self.mapping_formset.cleaned_data:
                if not row:
                    continue
                label = row["source_label"]
                parameters["sender_map"][label] = str(row["person"].pk)
                if row.get("fallback_recipient"):
                    parameters["recipient_map"][label] = str(
                        row["fallback_recipient"].pk
                    )
        for key in ("break_before", "join_before"):
            if self.cleaned_data.get(key):
                parameters[key] = [
                    source_id(selection(value)) for value in self.cleaned_data[key]
                ]
        return parameters

    def clean(self):
        data = super().clean()
        if (
            self.operation_name == "conversation_import"
            and not self.mapping_formset.is_valid()
        ):
            self.add_error(None, "Review the source account mappings below.")
        if self.operation_name == "conversation_import" and data.get("timezone"):
            try:
                ZoneInfo(data["timezone"])
            except ZoneInfoNotFoundError, ValueError:
                self.add_error("timezone", "Choose an explicit IANA source timezone.")
        chosen = set(data.get("sources", []))
        for key in ("break_before", "join_before"):
            if set(data.get(key, [])) - chosen:
                self.add_error(key, "Choose sources included in this analysis.")
        return data
