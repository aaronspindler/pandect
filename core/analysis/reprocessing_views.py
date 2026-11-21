"""Select active evidence types for a bounded reprocessing batch."""

from uuid import uuid4

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import DatabaseError
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.analysis.bulk import resume_case_analysis
from core.analysis.reprocessing import start_reprocessing
from core.evidence.queries import scoped_evidence
from core.evidence.types import KIND_LABELS


class ReprocessingForm(forms.Form):
    evidence_types = forms.MultipleChoiceField(
        label="Evidence types",
        widget=forms.CheckboxSelectMultiple,
        error_messages={"required": "Choose at least one evidence type."},
    )
    submission_id = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, *args, counts, **kwargs):
        super().__init__(*args, **kwargs)
        active_types = [kind for kind, count in counts.items() if count]
        selected = self["evidence_types"].value() if self.is_bound else ()
        self.fields["evidence_types"].choices = [
            (kind, f"{label} · {counts[kind]} active")
            for kind, label in KIND_LABELS.items()
            if kind in active_types or kind in (selected or ())
        ]
        if not self.is_bound:
            self.initial |= {"evidence_types": active_types, "submission_id": uuid4()}


@product_login_required
@require_POST
def resume(request, case_id, batch_id):
    batch, resumed = resume_case_analysis(request.user, case_id, batch_id)
    if resumed:
        messages.success(request, "Analysis preparation will resume.")
    return redirect(
        reverse("core:analysis:processing", args=[case_id])
        + f"?batch={batch.pk}#processing-history"
    )


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
        counts=counts,
    )
    if request.method == "POST" and form.is_valid():
        try:
            batch, created = start_reprocessing(
                request.user,
                case.pk,
                submission_id=form.cleaned_data["submission_id"],
                kinds=form.cleaned_data["evidence_types"],
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
