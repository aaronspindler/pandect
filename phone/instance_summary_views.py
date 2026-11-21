"""Review summaries for one saved instance or a conversation's missing summaries."""

from hashlib import sha256

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.analysis.bulk import start_case_analysis
from core.analysis.errors import AnalysisError, error_message
from core.analysis.execution import snapshot_execution
from core.analysis.readiness import configuration_readiness
from core.models import AnalysisRun, CaseAnalysisBatch
from people.models import Person
from phone.analysis_forms import InstanceSummaryForm
from phone.conversation_instances import get_session_instance, review_instance_page
from phone.conversations import conversation_list_page, pair_messages
from phone.instance_summaries import (
    OPERATION,
    pending_summaries,
    queue_summary,
    summary_limit,
    summary_originals,
)

SESSIONS_PER_REVIEW = 50


def _batch_url(batch):
    return reverse(
        "core:analysis:processing",
        args=(batch.case_id,),
        query={"batch": batch.pk},
        fragment="processing-history",
    )


def _active_batch_redirect(request, case, people, instance_id=None):
    batches = CaseAnalysisBatch.objects.filter(
        case=case, scope__type="instance_summaries", scope__people=people
    )
    if instance_id is None:
        batches = batches.filter(
            Q(status=CaseAnalysisBatch.Status.QUEUED)
            | Q(
                runs__status__in=(AnalysisRun.Status.QUEUED, AnalysisRun.Status.RUNNING)
            )
        )
    else:
        batches = batches.filter(
            status=CaseAnalysisBatch.Status.QUEUED,
            scope__instances__contains=[str(instance_id)],
        )
    active = batches.only("case_id").first()
    if active is not None:
        messages.info(request, "Instance summaries are already in progress.")
        return redirect(_batch_url(active))


def _conversation_review(request, case, pair, people, context):
    if response := _active_batch_redirect(request, case, people):
        return response

    page = review_instance_page(
        case, pair, request.GET.get("review_page"), SESSIONS_PER_REVIEW
    )
    missing = [instance for instance in page if instance["summary"] is None]
    pending = pending_summaries(case, missing)
    unrequested = [instance for instance in missing if instance["id"] not in pending]
    originals = summary_originals(
        pair, {pk for instance in unrequested for pk in instance["member_ids"]}
    )
    eligible = []
    oversized = 0
    for instance in unrequested:
        if summary_limit(instance["member_ids"], originals):
            oversized += 1
        else:
            eligible.append(instance)

    ids = [str(instance["id"]) for instance in eligible]
    revision = sha256("|".join(ids).encode()).hexdigest()
    submitting = request.method == "POST"
    scope_changed = submitting and request.POST.get("reviewed_scope") != revision
    form = (
        InstanceSummaryForm(
            request.POST if submitting and not scope_changed else None,
            actor=request.user,
            case=case,
            scope=("conversation", *people, revision),
        )
        if ids
        else None
    )
    if form is not None and form.is_bound and form.is_valid():
        # The worker revalidates each reviewed session and skips any work already
        # started between review and preparation.
        get_case(request.user, case.pk, edit=True, lock=True)
        if response := _active_batch_redirect(request, case, people):
            return response
        backend = form.cleaned_data["backend"]
        try:
            batch, created = start_case_analysis(
                request.user,
                case.pk,
                submission_id=form.cleaned_data["submission_id"],
                scope={
                    "type": "instance_summaries",
                    "people": people,
                    "instances": ids,
                    "backend": backend,
                    "execution": (
                        form.approved_execution
                        or snapshot_execution(OPERATION, {"backend": backend})
                    ),
                },
            )
        except ValidationError as error:
            form.add_error(None, error)
        else:
            if created:
                messages.success(request, "Instance summaries requested.")
            return redirect(_batch_url(batch))
    return render(
        request,
        "phone/instance_summary_review.html",
        context
        | {
            "bulk": True,
            "form": form,
            "instances": eligible,
            "review_page": page,
            "sessions_per_review": SESSIONS_PER_REVIEW,
            "eligible_count": len(eligible),
            "message_count": sum(instance["count"] for instance in eligible),
            "oversized": oversized,
            "pending_count": len(pending),
            "reviewed_scope": revision,
            "scope_changed": scope_changed,
        },
        status=409 if scope_changed else 400 if submitting else 200,
    )


@product_login_required
@require_http_methods(["GET", "POST"])
@transaction.atomic
def instance_summary(
    request, case_id, first_person_id, second_person_id, instance_id=None
):
    submitting = request.method == "POST"
    case = get_case(
        request.user, case_id, edit=True, lock=submitting and instance_id is not None
    )
    first = get_object_or_404(Person, case=case, pk=first_person_id)
    second = get_object_or_404(Person, case=case, pk=second_person_id)
    people = sorted(str(person.pk) for person in (first, second))
    pair = pair_messages(case, first, second)
    query = {"tab": "instances"}
    if instance_id is not None:
        query["instance"] = str(instance_id)
    elif (page := conversation_list_page(request.GET.get("page"))) > 1:
        query["page"] = page
    list_page = conversation_list_page(request.GET.get("list_page"))
    if list_page > 1:
        query["list_page"] = list_page
    context = case_context(case, request.user) | {
        "nav_section": "analysis",
        "first": first,
        "second": second,
        "back_url": reverse(
            "phone:conversation_detail",
            args=(case.pk, *sorted((first.pk, second.pk))),
            query=query,
        ),
        "readiness": configuration_readiness(OPERATION),
    }
    if instance_id is None:
        return _conversation_review(request, case, pair, people, context)

    instance = get_session_instance(case, pair, instance_id, include_statements=False)
    if instance["summary"] is not None:
        messages.info(
            request, "This conversation instance already has a saved summary."
        )
        return redirect(context["back_url"])
    if response := _active_batch_redirect(request, case, people, instance_id):
        return response
    if not submitting and (
        pending := pending_summaries(case, [instance]).get(instance["id"])
    ):
        messages.info(request, "This summary is already in progress.")
        return redirect("core:analysis:detail", case_id=case.pk, run_id=pending.pk)

    member_ids = instance["member_ids"]
    originals = summary_originals(pair, member_ids)
    limit_message = summary_limit(member_ids, originals)

    form = InstanceSummaryForm(
        request.POST if submitting else None,
        actor=request.user,
        case=case,
        scope=(str(instance_id),),
    )
    if submitting and not limit_message and form.is_valid():
        try:
            run, created = queue_summary(
                request.user,
                case,
                instance,
                backend=form.cleaned_data["backend"],
                submission_id=form.cleaned_data["submission_id"],
                execution=form.approved_execution,
            )
        except ValidationError as error:
            form.add_error(None, error)
        except AnalysisError as error:
            form.add_error(None, error_message(error.code))
        else:
            messages.success(
                request,
                "Instance summary queued."
                if created
                else "This summary was already queued.",
            )
            return redirect("core:analysis:detail", case_id=case.pk, run_id=run.pk)
    return render(
        request,
        "phone/instance_summary_review.html",
        context
        | {
            "form": form,
            "originals": [originals[pk] for pk in member_ids],
            "limit_message": limit_message,
        },
        status=400 if submitting else 200,
    )
