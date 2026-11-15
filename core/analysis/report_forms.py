"""Common current-field report filters without domain dispatch."""

from django import forms

from ui.forms import localize_datetime_labels


class BaseReportForm(forms.Form):
    state = forms.ChoiceField(
        choices=(
            ("active", "Active"),
            ("archived", "Archived"),
            ("all", "All evidence"),
        ),
        initial="active",
    )
    start = forms.DateTimeField(
        required=False,
        label="From (UTC)",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )
    end = forms.DateTimeField(
        required=False,
        label="Through (UTC)",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )

    def __init__(self, *args, case, **kwargs):
        super().__init__(*args, **kwargs)
        localize_datetime_labels(self)

    def clean(self):
        data = super().clean()
        if data.get("start") and data.get("end") and data["start"] > data["end"]:
            self.add_error("end", "The end must follow the start.")
        return data
