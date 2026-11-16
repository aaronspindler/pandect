from allauth.account.models import EmailAddress
from django import forms
from django.contrib.auth import get_user_model
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from .models import Case, CaseInvitation, CaseMembership


class CaseForm(forms.ModelForm):
    expected_updated_at = forms.CharField(widget=forms.HiddenInput, required=False)

    class Meta:
        model = Case
        fields = ("name",)
        labels = {"name": "Case name"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance._state.adding:
            self.fields["expected_updated_at"].required = True
            self.initial["expected_updated_at"] = self.instance.updated_at.isoformat()


class ConfirmForm(forms.Form):
    confirm = forms.BooleanField(label="I understand and want to continue.")


class MembershipRoleForm(forms.ModelForm):
    class Meta:
        model = CaseMembership
        fields = ("role",)
        labels = {"role": "Case access"}


class InvitationForm(forms.ModelForm):
    class Meta:
        model = CaseInvitation
        fields = ("email", "role")
        labels = {"email": "Email address", "role": "Case access"}

    def __init__(self, *args, case, **kwargs):
        super().__init__(*args, **kwargs)
        self.case = case

    def clean_email(self):
        email = self.cleaned_data["email"].strip().casefold()
        member_ids = self.case.memberships.values("user_id")
        matching_address = EmailAddress.objects.filter(
            user_id=OuterRef("pk"), email__iexact=email
        )
        if (
            get_user_model()
            .objects.filter(Q(pk=self.case.owner_id) | Q(pk__in=member_ids))
            .filter(Q(email__iexact=email) | Exists(matching_address))
            .exists()
        ):
            raise forms.ValidationError(
                "This person already has access. Use the collaborator controls."
            )
        pending = CaseInvitation.objects.filter(
            case=self.case,
            email__iexact=email,
            accepted_at__isnull=True,
            revoked_at__isnull=True,
            expires_at__gt=timezone.now(),
        )
        if pending.exists():
            raise forms.ValidationError(
                "An invitation is already pending. Resend it from Case settings."
            )
        return email


class InvitationRoleForm(forms.ModelForm):
    class Meta:
        model = CaseInvitation
        fields = ("role",)
        labels = {"role": "Case access"}
