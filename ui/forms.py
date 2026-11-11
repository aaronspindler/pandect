from django import forms
from django.core import signing


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
