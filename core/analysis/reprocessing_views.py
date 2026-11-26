"""Queue and recover case processing batches."""

from uuid import UUID, uuid4

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import DatabaseError
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.analysis.automatic import start_evidence_reprocessing
from core.analysis.bulk import resume_case_analysis
from core.analysis.errors import AnalysisError, error_message
from core.analysis.forms import EvidenceAnalysisForm
from core.analysis.reprocessing import start_reprocessing
from core.analysis.retries import start_failed_retries
from core.evidence.navigation import list_params
from core.evidence.queries import get_evidence, scoped_evidence
from core.evidence.types import KIND_LABELS
from core.evidence.workspace_views import render_detail


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
def retry_failed(request, case_id):
    url = reverse("core:analysis:processing", args=[case_id])
    try:
        submission_id = UUID(request.POST.get("submission_id", ""))
        after_batch = request.POST.get("after_batch")
        after_batch_id = UUID(after_batch) if after_batch else None
    except ValueError:
        get_case(request.user, case_id, edit=True)
        messages.error(request, "Reload Processing to try again.")
        return redirect(url + "?status=failed#processing-history")
    try:
        batch, created = start_failed_retries(
            request.user,
            case_id,
            submission_id=submission_id,
            after_batch_id=after_batch_id,
        )
    except ValidationError, DatabaseError:
        messages.error(request, "Retries could not be queued. Please try again.")
    else:
        if batch is not None:
            messages.success(
                request,
                "Failed check retries requested."
                if created
                else "This retry request has already been queued.",
            )
            return redirect(url + f"?batch={batch.pk}#processing-history")
        messages.info(request, "There are no failed checks to retry.")
    return redirect(url + "?status=failed#processing-history")


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


@product_login_required
@require_POST
def rerun_all(request, case_id):
    case = get_case(request.user, case_id, edit=True)
    form = EvidenceAnalysisForm(request.POST)
    if form.is_valid():
        try:
            _, created = start_reprocessing(
                request.user,
                case.pk,
                submission_id=form.cleaned_data["submission_id"],
                kinds=list(KIND_LABELS),
            )
        except ValidationError as error:
            messages.error(request, " ".join(error.messages))
        except DatabaseError:
            messages.error(request, "Analysis could not be queued. Please try again.")
        else:
            messages.success(
                request,
                "Analysis requested for all active evidence. "
                "Follow its progress in Processing."
                if created
                else "This analysis request has already been queued.",
            )
    else:
        messages.error(request, "This request expired. Please try again.")
    return redirect("core:analysis:processing", case_id=case.pk)


@product_login_required
@require_http_methods(["GET", "POST"])
def process_evidence(request, case_id, kind, evidence_id):
    case = get_case(request.user, case_id, edit=request.method == "POST")
    item = get_evidence(case, kind, evidence_id)
    params = list_params(request)
    if request.method == "GET" and request.GET.get("results_page"):
        params["results_page"] = request.GET["results_page"]
    detail_url = (
        reverse("core:evidence:detail", args=[case.pk, kind, item.pk])
        + ("?" + params.urlencode() if params else "")
        + "#evidence-findings-heading"
    )
    if request.method == "GET":
        return redirect(detail_url)
    form = EvidenceAnalysisForm(request.POST)
    if form.is_valid():
        try:
            batch, created = start_evidence_reprocessing(
                request.user,
                case.pk,
                kind,
                item.pk,
                submission_id=form.cleaned_data["submission_id"],
            )
        except (ValidationError, AnalysisError) as error:
            if isinstance(error, ValidationError):
                form.add_error(None, error)
            elif error.code == "source_archived":
                form.add_error(None, error_message(error.code))
            else:
                form.add_error(
                    None,
                    "A retained source is unavailable or changed. "
                    "Review the original and try again.",
                )
        else:
            messages.success(
                request,
                "Analysis requested."
                if created
                else "This analysis was already requested.",
            )
            return redirect(
                reverse(
                    "core:analysis:processing",
                    args=(case.pk,),
                    query={"batch": batch.pk},
                    fragment="processing-history",
                )
            )
    return render_detail(request, case, kind, item, analysis_form=form, status=400)
