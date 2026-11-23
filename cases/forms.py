from allauth.account.models import EmailAddress
from django import forms
from django.contrib.auth import get_user_model
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.utils.functional import cached_property

from core.analysis.execution import (
    INFERENCE_OPERATIONS,
    available_executions,
    execution_confirmation,
    external_model_choices,
)

from .models import Case, CaseInvitation, CaseMembership

CASE_ACCESS_HELP = (
    "Viewers can inspect and download. Editors can also add or edit records "
    "and archive or restore evidence."
)
ANALYSIS_MODEL_LABELS = {
    "transcription": "Recording transcription",
    "text_topics": "Topic summaries",
    "content_events": "Claims and commitments",
    "session_summaries": "Message-session summaries",
    "conversation_link": "Semantic conversation links",
    "question_synthesis": "Question-driven synthesis",
}


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


class CaseAIBackendForm(forms.Form):
    ai_backend = forms.ChoiceField(
        choices=Case.AIBackend.choices,
        label="AI backend",
    )

    def __init__(self, *args, case, **kwargs):
        self.case = case
        super().__init__(*args, initial={"ai_backend": case.ai_backend}, **kwargs)

    @cached_property
    def external_ai_available(self):
        return any(
            Case.AIBackend.GATEWAY
            in available_executions(
                self.case, operation, {"include_summary": True, "semantic_links": True}
            )
            for operation in INFERENCE_OPERATIONS
        )

    def clean_ai_backend(self):
        backend = self.cleaned_data["ai_backend"]
        if backend == Case.AIBackend.GATEWAY and not self.external_ai_available:
            raise forms.ValidationError(
                "Approve an external AI destination before choosing External AI."
            )
        return backend


class CaseExternalModelsForm(forms.Form):
    confirmation = forms.CharField(widget=forms.HiddenInput)

    def __init__(self, *args, case, **kwargs):
        saved = case.external_analysis_models
        options = {
            operation: external_model_choices(operation)
            for operation in ANALYSIS_MODEL_LABELS
        }
        self.expected_confirmation = execution_confirmation(
            "analysis.case.models", [str(case.pk)], [saved, options]
        )
        self.stale = False
        super().__init__(
            *args,
            initial={**saved, "confirmation": self.expected_confirmation},
            **kwargs,
        )
        self.rows = []
        for operation, label in ANALYSIS_MODEL_LABELS.items():
            models, selected = options[operation], saved.get(operation)
            if not models and not selected:
                continue
            default = next(iter(models), "Unavailable")
            field = None
            if len(models) > 1 or selected:
                choices = [("", f"Default · {default}")]
                choices.extend(
                    (model, model)
                    for model in models
                    if model != default or model == selected
                )
                if selected and selected not in models:
                    choices.append((selected, f"{selected} (unavailable)"))
                self.fields[operation] = forms.ChoiceField(
                    label=label, choices=choices, required=False
                )
                field = self[operation]
            self.rows.append((label, selected or default, field))

    @property
    def editable(self):
        return bool(self.visible_fields())

    def clean(self):
        values = super().clean()
        if not constant_time_compare(
            values.get("confirmation", ""), self.expected_confirmation
        ):
            self.stale = True
            self.data = self.data.copy()
            self.data["confirmation"] = self.expected_confirmation
            raise forms.ValidationError(
                "Compare your choices with the latest saved models, then save again."
            )
        return values


class MembershipRoleForm(forms.ModelForm):
    class Meta:
        model = CaseMembership
        fields = ("role",)
        labels = {"role": "Case access"}
        help_texts = {"role": CASE_ACCESS_HELP}


class InvitationForm(forms.ModelForm):
    class Meta:
        model = CaseInvitation
        fields = ("email", "role")
        labels = {"email": "Email address", "role": "Case access"}
        help_texts = {"role": CASE_ACCESS_HELP}

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
        help_texts = {"role": CASE_ACCESS_HELP}
