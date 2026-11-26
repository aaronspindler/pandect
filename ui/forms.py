from datetime import UTC

from django import forms
from django.core import signing
from django.utils import timezone


class UTCDateTimeField(forms.DateTimeField):
    """Parse and display a UTC wall time independently of the account timezone."""

    def to_python(self, value):
        with timezone.override(UTC):
            value = super().to_python(value)
        return value.astimezone(UTC) if value is not None else None

    def prepare_value(self, value):
        with timezone.override(UTC):
            return super().prepare_value(value)


def localize_datetime_labels(form):
    """Keep date/time input labels aligned with Django's active timezone."""
    for field in form.fields.values():
        if (
            isinstance(field, forms.DateTimeField)
            and not isinstance(field, UTCDateTimeField)
            and field.label
        ):
            field.label = field.label.replace(
                "(UTC", f"({timezone.get_current_timezone_name()}"
            )


class SignedRevisionForm(forms.ModelForm):
    """Preserve a draft when a UUID record's saved timestamp has changed."""

    revision = forms.CharField(widget=forms.HiddenInput)

    @classmethod
    def revision_for(cls, instance):
        return signing.dumps(
            {"id": str(instance.pk), "updated_at": instance.updated_at.isoformat()},
            salt=cls.revision_salt,
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stale = False
        if self.instance._state.adding:
            del self.fields["revision"]
        else:
            self.initial["revision"] = self.revision_for(self.instance)

    def clean_revision(self):
        value = self.cleaned_data["revision"]
        message = "Reload this form before saving changes."
        try:
            revision = signing.loads(value, salt=self.revision_salt)
        except signing.BadSignature as error:
            raise forms.ValidationError(message) from error
        if not isinstance(revision, dict) or revision.get("id") != str(
            self.instance.pk
        ):
            raise forms.ValidationError(message)
        self.stale = revision.get("updated_at") != self.instance.updated_at.isoformat()
        return value

    def clean(self):
        cleaned = super().clean()
        if self.stale:
            self.add_error(None, self.stale_message)
        return cleaned

    def refresh_revision(self):
        """Keep submitted fields and errors while issuing the current revision."""
        self.data = self.data.copy()
        self.data[self.add_prefix("revision")] = self.initial["revision"]

    @property
    def response_status(self):
        if self.stale:
            return 409
        return 400 if self.is_bound else 200
