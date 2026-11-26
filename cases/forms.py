from allauth.account.models import EmailAddress
from django import forms
from django.contrib.auth import get_user_model
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from core.analysis.execution import (
    execution_confirmation,
    external_model_options,
    gateway_execution,
    inference_purpose,
    model_form_value,
    pin_model_choice,
)
from core.analysis.gateway import gateway_route_configured
from core.analysis.model_catalog import reasoning_efforts

from .models import Case, CaseInvitation, CaseMembership

CASE_ACCESS_HELP = (
    "Viewers can inspect and download. Editors can also add or edit records "
    "and archive or restore evidence."
)
ANALYSIS_MODEL_LABELS = {
    "ocr": "Image and scanned page text",
    "plate_ocr": "Vehicle plate text",
    "claim_comparison": "Claim comparisons",
    "narrative_relationships": "Candidate actors and relationships",
    "text_topics": "Topic summaries",
    "content_events": "Claims and commitments",
    "session_summaries": "Message-session summaries",
    "conversation_link": "Semantic conversation links",
    "question_synthesis": "Question-driven synthesis",
    "evidence_chat": "Evidence chat",
    "transcription": "Recording transcription",
}
EFFORT_CHOICES = {
    "": "Default",
    "none": "None",
    "minimal": "Minimal",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "Extra high",
    "max": "Maximum",
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


class ModelSelect(forms.Select):
    def __init__(self, unavailable, efforts, defaults):
        super().__init__()
        self.unavailable = unavailable
        self.efforts = efforts
        self.defaults = defaults

    def create_option(self, name, value, *args, **kwargs):
        option = super().create_option(name, value, *args, **kwargs)
        if value in self.unavailable:
            option["attrs"]["disabled"] = True
        if efforts := self.efforts.get(value):
            option["attrs"]["data-efforts"] = " ".join(efforts)
            option["attrs"]["data-default-effort"] = self.defaults[value]
        return option


class CaseAISettingsForm(forms.Form):
    confirmation = forms.CharField(widget=forms.HiddenInput)

    def __init__(self, *args, case, **kwargs):
        self.case = case
        saved = case.external_analysis_models
        self.stale = False
        super().__init__(*args, initial=saved.copy(), **kwargs)
        self.rows = []
        inventory = {}
        self.available = {}
        self.efforts = {}
        for operation, label in ANALYSIS_MODEL_LABELS.items():
            purpose = inference_purpose(operation)
            default = gateway_execution(purpose, operation)
            options = external_model_options(operation)
            efforts = {"": reasoning_efforts(default)} | {
                option["value"]: option["efforts"] for option in options
            }
            defaults = {
                "": default.get("inference_settings", {})
                .get("reasoning", {})
                .get("effort", "")
            } | {option["value"]: option["default_effort"] for option in options}
            inventory[operation] = [default, options, efforts[""]]
            selected = model_form_value(operation, saved.get(operation, ""), case=case)
            self.initial[operation] = selected
            groups = {}
            names = {}
            available = set()
            for option in options:
                value = option["value"]
                name = option["label"]
                names[value] = name
                if option["available"]:
                    available.add(value)
                else:
                    name += " (setup required)"
                groups.setdefault(option["group"], []).append((value, name))
            default_label = (
                names.get(default["model"], default["model"]) or "Unavailable"
            )
            if not gateway_route_configured(purpose, default):
                default_label += " (setup required)"
            choices = [("", f"Default · {default_label}"), *groups.items()]
            if selected and selected not in names:
                saved_route = gateway_execution(purpose, operation, case=case)
                saved_label = (
                    f"Saved · {selected}"
                    if gateway_route_configured(purpose, saved_route)
                    else f"{selected} (unavailable)"
                )
                choices.append((selected, saved_label))
            unavailable = names.keys() - available - {selected}
            self.fields[operation] = forms.ChoiceField(
                label=label,
                choices=choices,
                widget=ModelSelect(unavailable, efforts, defaults),
                required=False,
            )
            self.available[operation] = available
            effort_name = f"{operation}_effort"
            saved_effort = case.external_analysis_efforts.get(operation, "")
            active_effort = (
                saved_effort if saved_effort in efforts.get(selected, ()) else ""
            )
            effort_field = None
            if purpose == "text" and (
                saved_effort
                or effort_name in self.data
                or any(efforts.get(value) for value in ("", selected, *available))
            ):
                if active_effort:
                    self.initial[effort_name] = active_effort
                self.fields[effort_name] = forms.ChoiceField(
                    label=f"Effort for {label}",
                    choices=EFFORT_CHOICES,
                    widget=forms.Select(attrs={"data-reasoning-effort": ""}),
                    required=False,
                )
                self.efforts[operation] = efforts
                effort_field = self[effort_name]
            displayed_effort = active_effort or defaults.get(selected, "")
            self.rows.append(
                (
                    label,
                    names.get(selected, selected) or default_label,
                    self[operation],
                    effort_field,
                    EFFORT_CHOICES.get(displayed_effort, "")
                    if displayed_effort
                    else "",
                )
            )
        from core.analysis.approval import hosted_destinations

        self.destinations = hosted_destinations()
        inventory["hosting"] = self.destinations
        self.expected_confirmation = execution_confirmation(
            "analysis.case.settings",
            [
                str(case.pk),
                str(case.external_ai_revision),
                case.external_ai_enabled,
                saved,
                case.external_analysis_efforts,
                case.external_analysis_approval,
            ],
            inventory,
        )
        self.initial["confirmation"] = self.expected_confirmation

    def clean(self):
        values = super().clean()
        if not constant_time_compare(
            values.get("confirmation", ""), self.expected_confirmation
        ):
            self.stale = True
            self.data = self.data.copy()
            self.data["confirmation"] = self.expected_confirmation
            raise forms.ValidationError(
                "Settings changed. Compare your choices with the latest saved "
                "settings, then save again."
            )
        for operation, allowed in self.available.items():
            value = values.get(operation)
            if value and value not in allowed and self.initial[operation] != value:
                self.add_error(
                    operation,
                    "This model needs operator setup before it can be selected.",
                )
        for operation, efforts in self.efforts.items():
            name = f"{operation}_effort"
            effort = values.get(name)
            model = (
                values.get(operation)
                if operation in self.data
                else self.initial[operation]
            )
            if model is not None and effort and effort not in efforts.get(model, ()):
                choices = ", ".join(
                    EFFORT_CHOICES[level] for level in ("", *efforts.get(model, ()))
                )
                self.add_error(name, f"Effort choices for this model: {choices}.")
        return values

    def proposed_models(self):
        models = self.case.external_analysis_models.copy()
        for operation in ANALYSIS_MODEL_LABELS:
            value = (
                self.cleaned_data[operation]
                if operation in self.data
                else self.initial[operation]
            )
            if not value:
                models.pop(operation, None)
            elif value in self.available[operation]:
                models[operation] = pin_model_choice(operation, value)
        return models

    def proposed_efforts(self):
        saved = self.case.external_analysis_efforts.copy()
        for operation, efforts in self.efforts.items():
            name = f"{operation}_effort"
            if name in self.data:
                if effort := self.cleaned_data[name]:
                    saved[operation] = effort
                else:
                    saved.pop(operation, None)
            elif operation in self.data:
                model = self.cleaned_data[operation]
                if model != self.initial[operation] and saved.get(
                    operation
                ) not in efforts.get(model, ()):
                    saved.pop(operation, None)
        return saved


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
