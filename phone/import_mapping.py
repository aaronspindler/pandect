"""Bounded, explicit account-label mappings for retained message imports."""

from django import forms
from django.forms import BaseFormSet, formset_factory

from people.models import Person


class ParticipantMappingForm(forms.Form):
    source_label = forms.CharField(label="Source account label", max_length=255)
    person = forms.ModelChoiceField(queryset=Person.objects.none(), label="Case person")
    fallback_recipient = forms.ModelChoiceField(
        queryset=Person.objects.none(),
        required=False,
        label="Recipient when the source omits one",
        help_text=(
            "Leave blank to retain those rows as unresolved. "
            "Never guessed from other labels."
        ),
    )

    def __init__(self, *args, case, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("person", "fallback_recipient"):
            self.fields[name].queryset = Person.objects.filter(case=case)


class ParticipantMappingFormSet(BaseFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        labels = set()
        for form in self.forms:
            label = form.cleaned_data.get("source_label")
            if label in labels:
                raise forms.ValidationError("Use each source account label only once.")
            if label:
                labels.add(label)


MappingFormSet = formset_factory(
    ParticipantMappingForm,
    formset=ParticipantMappingFormSet,
    extra=0,
    min_num=1,
    max_num=100,
    validate_min=True,
    validate_max=True,
    absolute_max=100,
)


def edit_rows(data):
    """Re-display submitted values as initial values without queuing analysis."""
    try:
        total = min(100, max(0, int(data.get("participants-TOTAL_FORMS", "0"))))
    except ValueError, TypeError:
        total = 0
    fields = ("source_label", "person", "fallback_recipient")
    rows = [
        {name: data.get(f"participants-{index}-{name}", "") for name in fields}
        for index in range(total)
    ]
    action = data.get("mapping_action", "")
    message = ""
    if action == "add" and total < 100:
        rows.append({})
    elif action.startswith("remove:"):
        try:
            index = int(action.split(":", 1)[1])
        except ValueError:
            index = -1
        if 0 <= index < len(rows) and len(rows) > 1:
            rows.pop(index)
        else:
            message = "Keep at least one mapping row."
    else:
        message = "Choose up to 100 source account mappings."
    return rows or [{}], message
