"""Case-scoped participant filters for deterministic call reports."""

from django import forms

from core.analysis.report_forms import BaseReportForm


class CallReportForm(BaseReportForm):
    participant_id = forms.ChoiceField(required=False, label="Participant")
    other_participant_id = forms.ChoiceField(
        required=False, label="Other participant in the pair"
    )

    def __init__(self, *args, case, **kwargs):
        super().__init__(*args, case=case, **kwargs)
        choices = [
            ("", "All participants"),
            *((str(person.pk), str(person)) for person in case.people.all()),
        ]
        self.fields["participant_id"].choices = choices
        self.fields["other_participant_id"].choices = choices

    def clean(self):
        data = super().clean()
        if data.get("other_participant_id") and not data.get("participant_id"):
            self.add_error("participant_id", "Choose both participants for a pair.")
        return data
