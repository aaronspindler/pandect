from django import forms
from django.core import signing
from django.core.exceptions import ValidationError

from .models import Person


def person_revision(person):
    return signing.dumps(
        {"id": str(person.pk), "updated_at": person.updated_at.isoformat()},
        salt="people.edit-revision",
    )


class PersonForm(forms.ModelForm):
    revision = forms.CharField(widget=forms.HiddenInput)

    class Meta:
        model = Person
        fields = (
            "first_name",
            "last_name",
            "phone_number",
            "email",
            "address",
            "notes",
        )
        widgets = {
            "first_name": forms.TextInput(attrs={"autocomplete": "off"}),
            "last_name": forms.TextInput(attrs={"autocomplete": "off"}),
            "phone_number": forms.TextInput(attrs={"inputmode": "tel"}),
            "address": forms.Textarea(attrs={"rows": 3}),
            "notes": forms.Textarea(attrs={"rows": 5}),
        }
        labels = {"notes": "Investigator notes"}
        help_texts = {
            "phone_number": "Preserve the source's formatting and extensions.",
            "notes": (
                "Record source context, uncertainty, and conflicting observations."
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stale = False
        if self.instance._state.adding:
            del self.fields["revision"]
        else:
            self.initial["revision"] = person_revision(self.instance)

    def clean_revision(self):
        value = self.cleaned_data["revision"]
        try:
            revision = signing.loads(value, salt="people.edit-revision")
        except signing.BadSignature as error:
            raise ValidationError("Reload this form before saving changes.") from error
        if not isinstance(revision, dict) or revision.get("id") != str(
            self.instance.pk
        ):
            raise ValidationError("Reload this form before saving changes.")
        if revision.get("updated_at") != self.instance.updated_at.isoformat():
            self.stale = True
        return value

    def clean(self):
        cleaned = super().clean()
        if self.stale:
            raise ValidationError(
                "This person has newer saved details. Review them below before "
                "saving your proposed changes."
            )
        return cleaned


class PersonDeleteForm(forms.Form):
    confirm = forms.BooleanField(
        label="I understand this permanently deletes this person record."
    )
