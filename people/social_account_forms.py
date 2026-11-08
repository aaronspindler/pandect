from django import forms
from django.core.signing import BadSignature, Signer

from .models import SocialAccount

REVISION_SALT = "people.social-account-revision"


def social_account_revision(account):
    return Signer(salt=REVISION_SALT).sign(
        f"{account.pk}:{account.updated_at.isoformat()}"
    )


class SocialAccountForm(forms.ModelForm):
    revision = forms.CharField(widget=forms.HiddenInput)

    class Meta:
        model = SocialAccount
        fields = ("platform", "username", "source")
        labels = {"source": "Source / citation"}
        help_texts = {
            "platform": "Enter the platform name, for example Instagram or Mastodon.",
            "username": "Enter the username as reported, including any leading @.",
        }
        widgets = {
            "username": forms.TextInput(
                attrs={"autocomplete": "off", "autocapitalize": "none"}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stale = False
        if self.instance._state.adding:
            del self.fields["revision"]
        else:
            self.initial["revision"] = social_account_revision(self.instance)

    def clean_revision(self):
        revision = self.cleaned_data["revision"]
        message = "Reload this form before saving changes."
        try:
            saved_value = Signer(salt=REVISION_SALT).unsign(revision)
        except BadSignature as error:
            raise forms.ValidationError(message) from error
        account_id, _, updated_at = saved_value.partition(":")
        if account_id != str(self.instance.pk):
            raise forms.ValidationError(message)
        self.stale = updated_at != self.instance.updated_at.isoformat()
        return revision

    def clean(self):
        cleaned = super().clean()
        if self.stale:
            self.add_error(
                None, "This social account has newer saved details. Review them below."
            )
        platform = cleaned.get("platform")
        username = cleaned.get("username")
        if (
            platform
            and username
            and SocialAccount.objects.filter(
                person=self.instance.person, platform=platform, username=username
            )
            .exclude(pk=self.instance.pk)
            .exists()
        ):
            self.add_error("username", "This person already has this social account.")
        return cleaned


class SocialAccountDeleteForm(forms.Form):
    revision = forms.CharField(widget=forms.HiddenInput)
    confirm = forms.BooleanField(
        label="I understand this permanently removes this social account."
    )
