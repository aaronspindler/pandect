"""Bind a form's external approval to the destination actually displayed."""

from uuid import uuid4

from django import forms
from django.utils.crypto import constant_time_compare
from django.utils.html import format_html

from core.analysis.execution import (
    execution_confirmation,
    execution_destination,
    inference_configured,
    snapshot_execution,
)

TRANSFER_CONTENT = {
    "text": "text and source references",
    "transcription": "recordings",
    "geocode": "addresses or coordinates",
    "map_context": "coordinates and search areas",
}


def _confirmation(form, submission_id):
    return execution_confirmation(
        "analysis.initial.execution",
        [*form.approval_context, str(submission_id)],
        form.external_execution,
    )


def prepare_external_approval(form, *, actor, case, operation, scope=()):
    form.approved_execution = None
    form.approval_context = [
        str(actor.pk) if actor else "",
        str(case.pk),
        operation,
        *scope,
    ]
    execution = snapshot_execution(
        operation,
        {"backend": "gateway", "include_summary": True, "semantic_links": True},
    )
    form.external_execution = execution
    form.fields["execution_confirmation"] = forms.CharField(
        required=False, widget=forms.HiddenInput
    )
    field = form.fields["external_processing"]
    destination = execution_destination(execution)
    readiness = (
        " Ask the operator to configure external AI for this analysis."
        if execution["backend"] == "gateway"
        and not inference_configured(execution, operation)
        else ""
    )
    field.help_text = format_html(
        "Sends selected {} to {}. Provider retention policies apply. "
        "Sent requests cannot be recalled.{}",
        TRANSFER_CONTENT[execution["purpose"]],
        destination or "a geographic lookup service the operator must configure",
        readiness,
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
