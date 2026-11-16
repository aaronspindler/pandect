"""Select and approve a bounded reprocessing batch for active case evidence."""

import hashlib
import json
from uuid import uuid4

from django import forms
from django.contrib import messages
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import DatabaseError
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.analysis.execution import gateway_destination, processing_external
from core.analysis.reprocessing import reprocessing_executions, start_reprocessing
from core.evidence.queries import scoped_evidence
from core.evidence.types import KIND_LABELS

CONFIRMATION_SALT = "analysis.reprocess.execution"


def _confirmation(actor, case, submission_id, executions):
    fingerprint = hashlib.sha256(
        json.dumps(executions, sort_keys=True).encode()
    ).hexdigest()
    return [str(actor.pk), str(case.pk), str(submission_id), fingerprint]


class ReprocessingForm(forms.Form):
    evidence_types = forms.MultipleChoiceField(
        label="Evidence types",
        widget=forms.CheckboxSelectMultiple,
        error_messages={"required": "Choose at least one evidence type."},
    )
    mode = forms.ChoiceField(
        label="Processing location",
        choices=(
            ("local", "Local"),
            ("external", "External / AI"),
            ("both", "Both local and external / AI"),
        ),
        initial="local",
        widget=forms.RadioSelect,
    )
    external_processing = forms.BooleanField(
        required=False,
        label="I approve external processing of the selected evidence types",
        help_text=(
            "Required for External / AI or Both. Selected text and recordings may "
            "be sent through the operator's Cloudflare AI Gateway; addresses and "
            "coordinates may be sent to configured lookup services. Operator and "
            "provider retention policies apply. Sent requests cannot be recalled."
        ),
    )
    submission_id = forms.UUIDField(widget=forms.HiddenInput)
    execution_confirmation = forms.CharField(widget=forms.HiddenInput)

    def __init__(self, *args, actor, case, counts, **kwargs):
        super().__init__(*args, **kwargs)
        self.actor, self.case = actor, case
        self.executions = reprocessing_executions()
        destinations = dict.fromkeys(
            gateway_destination(route)
            for route in self.executions.values()
            if route and route["backend"] == "gateway"
        )
        self.fields["external_processing"].help_text = (
            "; ".join(destinations)
            + ". "
            + self.fields["external_processing"].help_text
        )
        self.fields["evidence_types"].choices = [
            (kind, f"{label} · {counts[kind]} active")
            for kind, label in KIND_LABELS.items()
        ]
        if not self.is_bound:
            submission_id = uuid4()
            self.initial |= {
                "evidence_types": [kind for kind, count in counts.items() if count],
                "submission_id": submission_id,
                "execution_confirmation": signing.dumps(
                    _confirmation(actor, case, submission_id, self.executions),
                    salt=CONFIRMATION_SALT,
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
        try:
            reviewed = signing.loads(
                data.get("execution_confirmation", ""), salt=CONFIRMATION_SALT
            )
        except signing.BadSignature:
            reviewed = None
        if not submission_id or reviewed != _confirmation(
            self.actor, self.case, submission_id, self.executions
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
            values["execution_confirmation"] = signing.dumps(
                _confirmation(self.actor, self.case, submission_id, self.executions),
                salt=CONFIRMATION_SALT,
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
            "lookup_locations": [
                {
                    "label": label,
                    "external": processing_external(operation, {}),
                }
                for operation, label in (
                    ("geocode_locations", "Address and coordinate lookup"),
                    ("geographic_context", "Geographic context"),
                )
            ],
        },
    )
