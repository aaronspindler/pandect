"""Review and queue a summary of the exact members of a saved session."""

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models.functions import Length
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.analysis.errors import AnalysisError, error_message
from core.analysis.readiness import configuration_readiness
from core.analysis.services import start_run
from people.models import Person
from phone.analysis.ai import summary_chunks
from phone.analysis_forms import InstanceSummaryForm
from phone.conversation_instances import get_session_instance
from phone.conversations import conversation_list_page, pair_messages


@product_login_required
@require_http_methods(["GET", "POST"])
@transaction.atomic
def instance_summary(request, case_id, first_person_id, second_person_id, instance_id):
    submitting = request.method == "POST"
    case = get_case(request.user, case_id, edit=True, lock=submitting)
    first = get_object_or_404(Person, case=case, pk=first_person_id)
    second = get_object_or_404(Person, case=case, pk=second_person_id)
    pair = pair_messages(case, first, second)
    instance = get_session_instance(case, pair, instance_id)
    query = {"tab": "instances", "instance": str(instance_id)}
    list_page = conversation_list_page(request.GET.get("list_page"))
    if list_page > 1:
        query["list_page"] = list_page
    back_url = reverse(
        "phone:conversation_detail",
        args=(case.pk, *sorted((first.pk, second.pk))),
        query=query,
    )
    if instance["summary"] is not None:
        messages.info(
            request, "This conversation instance already has a saved summary."
        )
        return redirect(back_url)

    member_ids = instance["member_ids"]
    rows = (
        pair.filter(pk__in=member_ids)
        .annotate(characters=Length("body"))
        .values("pk", "title", "characters")
    )
    originals = {row["pk"]: row for row in rows}
    if len(originals) != len(member_ids):
        raise Http404("The messages in this instance have changed.")
    ordered = [originals[pk] for pk in member_ids]
    limit_message = ""
    try:
        max_sources = settings.PANDECT_ANALYSIS_MAX_SOURCES
        if len(member_ids) > max_sources:
            raise ValidationError(
                f"One analysis supports at most {max_sources} messages."
            )
        summary_chunks([[(row["pk"], row["characters"]) for row in ordered]])
    except ValidationError as error:
        limit_message = error.messages[0]

    form = InstanceSummaryForm(
        request.POST if submitting else None,
        actor=request.user,
        case=case,
        instance_id=instance_id,
    )
    if submitting and not limit_message and form.is_valid():
        saved = instance["parameters"]
        parameters = {
            key: saved[key]
            for key in ("gap_minutes", "greeting_boundaries")
            if key in saved
        }
        for key in ("break_before", "join_before"):
            adjustments = [
                source_id
                for source_id in instance["source_ids"]
                if source_id in saved.get(key, ())
            ]
            if adjustments:
                parameters[key] = adjustments
        try:
            run, created = start_run(
                request.user,
                case.pk,
                "session_summaries",
                [{"kind": "text-message", "id": str(pk)} for pk in member_ids],
                parameters=parameters | {"backend": form.cleaned_data["backend"]},
                submission_id=form.cleaned_data["submission_id"],
                external_processing=form.cleaned_data["external_processing"],
                approved_execution=form.approved_execution,
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
        case_context(case, request.user)
        | {
            "nav_section": "analysis",
            "form": form,
            "first": first,
            "second": second,
            "back_url": back_url,
            "originals": ordered,
            "limit_message": limit_message,
            "readiness": configuration_readiness("session_summaries"),
        },
        status=400 if submitting else 200,
    )
