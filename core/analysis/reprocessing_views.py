"""Select and approve a bounded reprocessing batch for active case evidence."""

from uuid import uuid4

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import DatabaseError
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.crypto import constant_time_compare
from django.utils.html import format_html, format_html_join
from django.views.decorators.http import require_http_methods

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.analysis.approval import TRANSFER_CONTENT
from core.analysis.execution import execution_confirmation, execution_destination
from core.analysis.reprocessing import (
    MODE_LABELS,
    configured_reprocessing_executions,
    reprocessing_routes,
    start_reprocessing,
)
from core.evidence.queries import scoped_evidence
from core.evidence.types import KIND_LABELS

CONFIRMATION_SALT = "analysis.reprocess.execution"


def _confirmation(actor, case, submission_id, executions):
    return execution_confirmation(
        CONFIRMATION_SALT, [str(actor.pk), str(case.pk), str(submission_id)], executions
    )


class ReprocessingForm(forms.Form):
    evidence_types = forms.MultipleChoiceField(
        label="Evidence types",
        widget=forms.CheckboxSelectMultiple,
        error_messages={"required": "Choose at least one evidence type."},
    )
    mode = forms.ChoiceField(
        label="Processing location",
        choices=MODE_LABELS.items(),
        initial="local",
        widget=forms.RadioSelect,
    )
    external_processing = forms.BooleanField(
        required=False,
        label="I approve external processing of the selected evidence types",
        help_text=(
            "Required when you select external services. "
            "Provider retention policies apply."
        ),
    )
    submission_id = forms.UUIDField(widget=forms.HiddenInput)
    execution_confirmation = forms.CharField(widget=forms.HiddenInput)

    def __init__(self, *args, actor, case, counts, **kwargs):
        super().__init__(*args, **kwargs)
        self.actor, self.case = actor, case
        self.executions = configured_reprocessing_executions()
        active_types = [kind for kind, count in counts.items() if count]
        external_routes = {
            mode: [
                route
                for route in reprocessing_routes(
                    active_types, mode, self.executions
                ).values()
                if route["location"] == "external"
            ]
            for mode in ("external", "both")
        }
        self.fields["mode"].choices = [
            (mode, label)
            for mode, label in MODE_LABELS.items()
            if mode == "local" or external_routes[mode]
        ]
        destinations = dict.fromkeys(
            (
                TRANSFER_CONTENT[route["purpose"]].capitalize(),
                execution_destination(route),
            )
            for routes in external_routes.values()
            for route in routes
        )
        self.external_available = bool(destinations)
        if not destinations:
            self.fields["mode"].widget = forms.HiddenInput()
            self.fields["mode"].error_messages["invalid_choice"] = (
                "Only local checks are available. Review and submit again."
            )
            self.fields["external_processing"].widget = forms.HiddenInput()
            self.fields["external_processing"].disabled = True
        field = self.fields["external_processing"]
        if destinations:
            field.help_text = format_html(
                "{} {}",
                format_html_join(" ", "{} may be sent to {}.", destinations),
                field.help_text,
            )
        selected = self["evidence_types"].value() if self.is_bound else ()
        self.fields["evidence_types"].choices = [
            (kind, f"{label} · {counts[kind]} active")
            for kind, label in KIND_LABELS.items()
            if kind in active_types or kind in (selected or ())
        ]
        if not self.is_bound:
            submission_id = uuid4()
            self.initial |= {
                "evidence_types": active_types,
                "submission_id": submission_id,
                "execution_confirmation": _confirmation(
                    actor, case, submission_id, self.executions
                ),
            }

    def clean(self):
        data = super().clean()
        if data.get("mode") in {"external", "both"} and not data.get(
            "external_processing"
        ):
            self.add_error(
                "external_processing", "Approve external processing to continue."
            )
        submission_id = data.get("submission_id")
        if not submission_id or not constant_time_compare(
            data.get("execution_confirmation") or "",
            _confirmation(self.actor, self.case, submission_id, self.executions),
        ):
            self.add_error(
                None,
                "Processing settings changed or this request expired. "
                "Review the current settings and submit again.",
            )
            # Keep the proposed choices and issue a fresh review identity.
            values = self.data.copy()
            submission_id = uuid4()
            values["submission_id"] = str(submission_id)
            values["execution_confirmation"] = _confirmation(
                self.actor, self.case, submission_id, self.executions
            )
            self.data = values
            values.pop("external_processing", None)
            data["external_processing"] = False
        return data


@product_login_required
@require_http_methods(["GET", "POST"])
def reprocess(request, case_id):
    case = get_case(request.user, case_id, edit=True)
    counts = {
        kind: scoped_evidence(case, kind).filter(is_archived=False).count()
        for kind in KIND_LABELS
    }
    form = ReprocessingForm(
        request.POST if request.method == "POST" else None,
        actor=request.user,
        case=case,
        counts=counts,
    )
    if request.method == "POST" and form.is_valid():
        try:
            batch, created = start_reprocessing(
                request.user,
                case.pk,
                submission_id=form.cleaned_data["submission_id"],
                kinds=form.cleaned_data["evidence_types"],
                mode=form.cleaned_data["mode"],
                external_processing=form.cleaned_data["external_processing"],
                executions=form.executions,
            )
        except ValidationError as error:
            form.add_error(None, error)
        except DatabaseError:
            form.add_error(None, "Reprocessing could not be queued. Please try again.")
        else:
            messages.success(
                request,
                "Reprocessing requested. Follow preparation and results below."
                if created
                else "This reprocessing request has already been queued.",
            )
            return redirect(
                reverse("core:analysis:processing", args=[case.pk])
                + f"?batch={batch.pk}#processing-history"
            )
    return render(
        request,
        "core/analysis/reprocess.html",
        case_context(case, request.user)
        | {
            "nav_section": "processing",
            "form": form,
            "active_count": sum(counts.values()),
        },
    )
