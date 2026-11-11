from django import forms

from people.models import Person
from ui.forms import SignedRevisionForm


class PersonForm(SignedRevisionForm):
    revision_salt = "people.edit-revision"
    stale_message = (
        "This person has newer saved details. Review them below before "
        "saving your proposed changes."
    )

    class Meta:
        model = Person
        fields = (
            "first_name",
            "last_name",
            "phone_number",
            "email",
            "address",
        )
        widgets = {
            "first_name": forms.TextInput(attrs={"autocomplete": "off"}),
            "last_name": forms.TextInput(attrs={"autocomplete": "off"}),
            "phone_number": forms.TextInput(attrs={"inputmode": "tel"}),
            "address": forms.Textarea(attrs={"rows": 3}),
        }
        help_texts = {
            "phone_number": "Preserve the source's formatting and extensions.",
        }


class PersonDeleteForm(forms.Form):
    confirm = forms.BooleanField(
        label="I understand this permanently deletes this person record."
    )
