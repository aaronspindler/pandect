from zoneinfo import available_timezones

from django import forms

from .models import User


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
