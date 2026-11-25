"""Review summaries for one saved instance or a conversation's missing summaries."""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.analysis.errors import AnalysisError, error_message
from core.analysis.execution import selected_ai_executions
from core.analysis.forms import EvidenceAnalysisForm
from core.analysis.readiness import configuration_readiness
from core.models import AnalysisRun
from people.models import Person
from phone.conversation_instances import get_session_instance, review_instance_page
from phone.conversations import conversation_list_page, pair_messages
from phone.instance_summaries import (
    OPERATION,
    active_summary_batch,
    pending_summaries,
    request_summaries,
    review_summary_candidates,
    submitted_summary_batch,
    summary_limit,
    summary_originals,
    summary_review_revision,
)
from phone.saved_summary_ranges import session_reports, summary_gap, summary_members

SESSIONS_PER_REVIEW = 50


def _batch_url(batch):
    return reverse(
        "core:analysis:processing",
        args=(batch.case_id,),
        query={"batch": batch.pk},
        fragment="processing-history",
    )


def _active_batch_redirect(request, case, people, executions, instance_id=None):
    batch = active_summary_batch(case, people, executions, instance_id)
    if batch is not None:
        messages.info(request, "Instance summaries are already in progress.")
        return redirect(_batch_url(batch))


def _conversation_review(request, case, pair, people, context, executions):
    if response := _active_batch_redirect(request, case, people, executions):
        return response

    page = review_instance_page(
        case,
        pair,
        request.GET.get("review_page"),
        SESSIONS_PER_REVIEW,
        executions=executions,
    )
    eligible, oversized, pending_count = review_summary_candidates(
        case, pair, page, executions
    )

    ids = [str(instance["id"]) for instance in eligible]
    revision = summary_review_revision(case, ids, executions)
    submitting = request.method == "POST"
    scope_changed = submitting and request.POST.get("reviewed_scope") != revision
    form = (
        EvidenceAnalysisForm(request.POST if submitting and not scope_changed else None)
        if ids
        else None
    )
    if form is not None and form.is_bound and form.is_valid():
        # The worker revalidates each reviewed session and skips any work already
        # started between review and preparation.
        try:
            batch, created = request_summaries(
                request.user,
                case,
                people,
                ids,
                executions,
                form.cleaned_data["submission_id"],
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
            "pending_count": pending_count,
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
    case = get_case(request.user, case_id, edit=True, lock=submitting)
    first = get_object_or_404(Person, case=case, pk=first_person_id)
    second = get_object_or_404(Person, case=case, pk=second_person_id)
    people = sorted(str(person.pk) for person in (first, second))
    if submitting and (
        batch := submitted_summary_batch(
            request.user,
            case,
            people,
            instance_id,
            submission_id=request.POST.get("submission_id", ""),
            reviewed_scope=request.POST.get("reviewed_scope"),
        )
    ):
        return redirect(_batch_url(batch))
    pair = pair_messages(case, first, second)
    query = {"tab": "instances"}
    if instance_id is not None:
        query["instance"] = str(instance_id)
    elif (page := conversation_list_page(request.GET.get("page"))) > 1:
        query["page"] = page
    list_page = conversation_list_page(request.GET.get("list_page"))
    if list_page > 1:
        query["list_page"] = list_page
    executions = selected_ai_executions(case, OPERATION, {})
    context = case_context(case, request.user) | {
        "nav_section": "analysis",
        "first": first,
        "second": second,
        "back_url": reverse(
            "phone:conversation_detail",
            args=(case.pk, *sorted((first.pk, second.pk))),
            query=query,
        ),
        "execution": next(iter(executions.values()), None),
        "readiness": ""
        if executions
        else configuration_readiness(OPERATION, case=case),
        "available": bool(executions),
    }
    if instance_id is None:
        return _conversation_review(request, case, pair, people, context, executions)

    instance = get_session_instance(
        case, pair, instance_id, include_statements=False, executions=executions
    )
    if executions and not instance["summary_missing"]:
        messages.info(
            request, "This conversation instance already has a saved summary."
        )
        return redirect(context["back_url"])
    if response := _active_batch_redirect(
        request, case, people, executions, instance_id
    ):
        return response
    pending = pending_summaries(case, [instance], executions).get(instance["id"], {})
    if pending:
        messages.info(request, "This session summary is already in progress.")
        return redirect(
            "core:analysis:detail",
            case_id=case.pk,
            run_id=next(iter(pending.values())).pk,
        )

    if saved := instance.get("saved_session"):
        originals_page = Paginator(
            saved.members.filter(case=case)
            .order_by("ordinal")
            .values_list("message_id", flat=True),
            50,
        ).get_page(request.GET.get("message_page"))
        member_ids = list(originals_page.object_list)
        limit_message = ""
    else:
        originals_page = None
        member_ids = instance["member_ids"]
    originals = summary_originals(pair, member_ids)
    if not saved:
        limit_message = summary_limit(member_ids, originals)

    ids = [str(instance["id"])]
    revision = summary_review_revision(case, ids, executions)
    can_queue = bool(executions) and not limit_message
    scope_changed = (
        submitting
        and not limit_message
        and request.POST.get("reviewed_scope") != revision
    )
    form = EvidenceAnalysisForm(
        request.POST if submitting and not scope_changed else None
    )
    if submitting and can_queue and not scope_changed and form.is_valid():
        try:
            batch, created = request_summaries(
                request.user,
                case,
                people,
                ids,
                executions,
                form.cleaned_data["submission_id"],
                single=True,
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
            return redirect(_batch_url(batch))
    return render(
        request,
        "phone/instance_summary_review.html",
        context
        | {
            "form": form,
            "originals": [originals[pk] for pk in member_ids],
            "message_count": instance["count"],
            "originals_page": originals_page,
            "limit_message": limit_message,
            "reviewed_scope": revision,
            "scope_changed": scope_changed,
        },
        status=409 if scope_changed else 400 if submitting else 200,
    )


@product_login_required
@require_http_methods(["GET"])
def session_summary_coverage(
    request, case_id, first_person_id, second_person_id, instance_id
):
    from phone.conversation_instances import instance_url
    from phone.saved_sessions import valid_sessions

    case = get_case(request.user, case_id)
    pair = pair_messages(case, first_person_id, second_person_id)
    saved = get_object_or_404(valid_sessions(case, pair), pk=instance_id)
    members = summary_members(saved, case)
    page = Paginator(members, 50).get_page(request.GET.get("page"))
    rows = {}
    for ordinal, _, body, archived in page:
        rows[ordinal] = (summary_gap(body, archived) or "Awaiting summary", None)
    # Prefer confirmed coverage. Otherwise show the newest attempt for each
    # original on this page, including failed or partially summarized ranges.
    seen = set()
    reports = (
        session_reports(case, saved)
        .filter(
            parameters___saved_session_range__first__lte=page.end_index(),
            parameters___saved_session_range__last__gte=page.start_index(),
        )
        .order_by("-created_at", "pk")
        .values_list(
            "pk",
            "status",
            "parameters___saved_session_range",
            "result__data__metrics__range_covered",
            "result__case_id",
        )
    )
    covered = set()
    for run_id, status, scope, complete, result_case in reports.iterator(chunk_size=50):
        if result_case not in (None, case.pk):
            continue
        verified = status == "complete" and complete is True and result_case == case.pk
        for ordinal in rows.keys() - covered:
            if not scope["first"] <= ordinal <= scope["last"] or (
                ordinal in seen and not verified
            ):
                continue
            seen.add(ordinal)
            if verified:
                covered.add(ordinal)
                label = "Covered"
            elif status in ("complete", "partial"):
                label = "Partial coverage"
            else:
                label = AnalysisRun.Status(status).label
            rows[ordinal] = (label, run_id)
    ranges = []
    for ordinal, (label, run_id) in rows.items():
        if ranges and (label, run_id) == (ranges[-1]["label"], ranges[-1]["run_id"]):
            ranges[-1]["last"] = ordinal
        else:
            ranges.append(
                {"first": ordinal, "last": ordinal, "label": label, "run_id": run_id}
            )
    return render(
        request,
        "phone/session_summary_coverage.html",
        case_context(case, request.user)
        | {
            "nav_section": "analysis",
            "session": saved,
            "ranges": ranges,
            "page": page,
            "back_url": instance_url(
                case, (first_person_id, second_person_id), instance_id
            ),
        },
    )
