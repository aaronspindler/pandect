"""Bind a form's external approval to the destination actually displayed."""

import json
from uuid import uuid4

from django import forms
from django.utils.crypto import constant_time_compare, salted_hmac

from core.analysis.execution import gateway_destination, snapshot_execution


def _confirmation(form, submission_id):
    value = json.dumps(
        [*form.approval_context, str(submission_id), form.external_execution],
        sort_keys=True,
    )
    return salted_hmac(
        "analysis.initial.execution", value, algorithm="sha256"
    ).hexdigest()


def prepare_external_approval(form, *, actor, case, operation):
    form.approved_execution = None
    form.approval_context = [str(actor.pk) if actor else "", str(case.pk), operation]
    form.external_execution = snapshot_execution(
        operation,
        {"backend": "gateway", "include_summary": True, "semantic_links": True},
    )
    form.fields["execution_confirmation"] = forms.CharField(
        required=False, widget=forms.HiddenInput
    )
    field = form.fields["external_processing"]
    field.help_text = (
        gateway_destination(form.external_execution) + ". " + field.help_text
    )
    if not form.is_bound:
        submission_id = form.initial.get("submission_id") or uuid4()
        form.initial |= {
            "submission_id": submission_id,
            "execution_confirmation": _confirmation(form, submission_id),
        }


def validate_external_approval(form, data):
    submission_id = data.get("submission_id")
    expected = _confirmation(form, submission_id)
    if submission_id and constant_time_compare(
        data.get("execution_confirmation") or "", expected
    ):
        form.approved_execution = form.external_execution
        return
    form.add_error(
        None,
        "Processing settings changed or this request expired. "
        "Review the destination and approve external processing again.",
    )
    values = form.data.copy()
    submission_id = uuid4()
    values["submission_id"] = str(submission_id)
    values["execution_confirmation"] = _confirmation(form, submission_id)
    values.pop("external_processing", None)
    data["external_processing"] = False
    form.data = values
