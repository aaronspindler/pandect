"""Deliberate question synthesis from an exact, shared selection revision."""

from uuid import uuid4

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.analysis.errors import AnalysisError, error_message
from core.analysis.readiness import configuration_readiness
from core.analysis.selections import check_revision, resolve_selection, revision_token
from core.analysis.services import start_run
from core.models.selection import SavedSelection


class SynthesisForm(forms.Form):
    question = forms.CharField(
        max_length=2000,
        widget=forms.Textarea,
        help_text="Ask about the selected material. The answer will separate "
        "supported observations, contrary evidence, and unknowns.",
    )
    backend = forms.ChoiceField(
        label="Processing location",
        choices=(
            ("local", "This server or private network"),
            ("gateway", "External AI service"),
        ),
        initial="local",
    )
    external_processing = forms.BooleanField(
        required=False,
        label="I approve external processing of this selection",
        help_text="External AI sends selected text, selected findings, and "
        "their review context through the operator’s Cloudflare AI "
        "Gateway. Raw files are not sent. Operator retention "
        "policies apply.",
    )
    submission_id = forms.UUIDField(initial=uuid4, widget=forms.HiddenInput)
    revision = forms.CharField(widget=forms.HiddenInput)


@product_login_required
@require_http_methods(["GET", "POST"])
def create(request, case_id, selection_id):
    case = get_case(request.user, case_id, edit=True)
    selection = get_object_or_404(SavedSelection, pk=selection_id, case=case)
    form = SynthesisForm(
        request.POST or None, initial={"revision": revision_token(selection)}
    )
    status = 200
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                case = get_case(request.user, case.pk, edit=True, lock=True)
                selection = SavedSelection.objects.select_for_update().get(
                    pk=selection.pk, case=case
                )
                check_revision(selection, form.cleaned_data["revision"])
                resolved = resolve_selection(
                    selection, case, require_current_review=True
                )
                if resolved["unavailable"]:
                    raise ValidationError(
                        "Resolve unavailable selection members before synthesis."
                    )
                snapshots = resolved["snapshots"]
                choices = [
                    {
                        "kind": entry["kind"],
                        "id": entry["evidence_id"],
                        **{
                            key: entry[key]
                            for key in (
                                "attachment_id",
                                "result_id",
                                "geocode_candidate",
                            )
                            if entry.get(key) is not None
                        },
                    }
                    for entry in snapshots
                ]
                parameters = {
                    "question": form.cleaned_data["question"],
                    "backend": form.cleaned_data["backend"],
                    "text_source_ids": [
                        str(row["source"].id)
                        for row in resolved["members"]
                        if row["type"] == "source"
                    ],
                    "finding_refs": [
                        {
                            "result_id": row["member"]["result_id"],
                            "item_index": row["member"]["item_index"],
                        }
                        for row in resolved["members"]
                        if row["type"] == "finding"
                    ],
                }
                run, created = start_run(
                    request.user,
                    case.pk,
                    "question_synthesis",
                    choices,
                    parameters=parameters,
                    external_processing=form.cleaned_data["external_processing"],
                    submission_id=form.cleaned_data["submission_id"],
                )
                if {entry["id"]: entry for entry in run.inputs} != {
                    entry["id"]: entry for entry in snapshots
                }:
                    raise ValidationError(
                        "A selected source approval changed. Review the exact "
                        "selection before starting another run."
                    )
        except (ValidationError, AnalysisError) as error:
            form.add_error(
                None,
                error
                if isinstance(error, ValidationError)
                else error_message(error.code),
            )
            status = 400
        else:
            messages.success(
                request,
                "Synthesis queued."
                if created
                else "This synthesis was already queued.",
            )
            return redirect("core:analysis:detail", case_id=case.pk, run_id=run.pk)
    elif request.method == "POST":
        status = 400
    return render(
        request,
        "core/analysis/synthesis.html",
        case_context(case, request.user)
        | {
            "selection": selection,
            "form": form,
            "nav_section": "selections",
            "configuration_readiness": configuration_readiness("question_synthesis"),
        },
        status=status,
    )
