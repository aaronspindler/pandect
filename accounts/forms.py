from zoneinfo import available_timezones

from allauth.account.forms import LoginForm
from django import forms

from .models import User


class PandectLoginForm(LoginForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        login = self.fields["login"]
        login.label = "Username or email"
        login.widget.attrs.pop("placeholder", None)
        login.widget.attrs["autocomplete"] = "username"
        if "remember" in self.fields:
            self.fields["remember"].label = "Keep me signed in"


class TimezoneForm(forms.ModelForm):
    timezone = forms.ChoiceField(
        required=False,
        label="Timezone",
        choices=[
            ("", "Automatic — use this device's timezone"),
            *((name, name.replace("_", " ")) for name in sorted(available_timezones())),
        ],
        help_text="Used for displayed times and date/time fields.",
    )

    class Meta:
        model = User
        fields = ("timezone",)
