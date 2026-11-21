"""Prepare bounded summary runs for exact saved conversation instances."""

from uuid import uuid5

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.db.models.functions import Length
from django.http import Http404

from core.analysis.errors import AnalysisError
from core.analysis.execution import inference_configured, snapshot_execution
from core.analysis.services import start_run
from core.models import AnalysisRun
from people.models import Person
from phone.analysis.ai import summary_chunks
from phone.analysis.deterministic.conversations import DEFAULT_SESSION_GAP_MINUTES
from phone.conversation_instances import get_session_instance
from phone.conversations import pair_messages

OPERATION = "session_summaries"
INSTANCES_PER_STEP = 10


def summary_originals(pair, member_ids):
    return {
        row["pk"]: row
        for row in pair.filter(pk__in=member_ids)
        .annotate(characters=Length("body"))
        .values("pk", "title", "characters")
    }


def summary_limit(member_ids, originals):
    if not originals.keys() >= set(member_ids):
        raise Http404("The messages in this instance have changed.")
    max_sources = settings.PANDECT_ANALYSIS_MAX_SOURCES
    if len(member_ids) > max_sources:
        return f"One analysis supports at most {max_sources} messages."
    try:
        summary_chunks([[(pk, originals[pk]["characters"]) for pk in member_ids]])
    except ValidationError as error:
        return error.messages[0]
    return ""


def _summary_parameters(instance):
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
    return parameters


def _grouping_key(parameters):
    gap = parameters.get("gap_minutes", DEFAULT_SESSION_GAP_MINUTES)
    breaks = parameters.get("break_before", [])
    joins = parameters.get("join_before", [])
    if type(gap) not in (float, int) or not all(
        isinstance(adjustments, list) for adjustments in (breaks, joins)
    ):
        return None
    return (
        gap,
        bool(parameters.get("greeting_boundaries", True)),
        frozenset(map(str, breaks)),
        frozenset(map(str, joins)),
    )


def pending_summaries(case, instances):
    """Find in-flight runs with the same originals and grouping settings."""
    if not instances:
        return {}
    memberships = {
        frozenset(str(pk) for pk in instance["member_ids"]): (
            instance["id"],
            _grouping_key(_summary_parameters(instance)),
        )
        for instance in instances
    }
    candidates = Q(pk__in=[])
    for first_id in {str(instance["member_ids"][0]) for instance in instances}:
        candidates |= Q(
            inputs__contains=[{"kind": "text-message", "evidence_id": first_id}]
        )
    pending = {}
    for run in (
        AnalysisRun.objects.filter(
            candidates,
            case=case,
            operation=OPERATION,
            status__in=(AnalysisRun.Status.QUEUED, AnalysisRun.Status.RUNNING),
        )
        .only("inputs", "parameters")
        .iterator(chunk_size=10)
    ):
        if not all(
            isinstance(source, dict)
            and source.get("kind") == "text-message"
            and isinstance(source.get("evidence_id"), str)
            and not source.get("result_id")
            and not source.get("attachment_id")
            for source in run.inputs
        ):
            continue
        membership = frozenset(source.get("evidence_id") for source in run.inputs)
        if len(membership) != len(run.inputs) or membership not in memberships:
            continue
        instance_id, grouping = memberships[membership]
        if grouping is not None and grouping == _grouping_key(run.parameters):
            pending.setdefault(instance_id, run)
            if len(pending) == len(memberships):
                break
    return pending


def queue_summary(
    actor, case, instance, *, backend, submission_id, execution, batch=None
):
    # Both the single-request view and batch preparation hold the case lock.
    if pending := pending_summaries(case, [instance]).get(instance["id"]):
        return pending, False
    return start_run(
        actor,
        case.pk,
        OPERATION,
        [{"kind": "text-message", "id": str(pk)} for pk in instance["member_ids"]],
        parameters=_summary_parameters(instance) | {"backend": backend},
        submission_id=submission_id,
        external_processing=backend == "gateway",
        approved_execution=execution,
        batch=batch,
        require_configured_inference=True,
    )


def prepare_instance_summaries(batch, case):
    """Queue one run per reviewed instance, without merging overlapping groups."""
    scope = batch.scope
    backend = scope["backend"]
    if snapshot_execution(OPERATION, {"backend": backend}) != scope["execution"]:
        raise AnalysisError("execution_changed")
    if not inference_configured(scope["execution"], OPERATION):
        raise AnalysisError(
            "local_ai_not_configured"
            if backend == "local"
            else "gateway_not_configured"
        )
    people = [Person.objects.filter(case=case, pk=pk).first() for pk in scope["people"]]
    if None in people:
        raise AnalysisError("source_unavailable")
    pair = pair_messages(case, *people)
    start = batch.processed_sources
    reviewed = scope["instances"][start : start + INSTANCES_PER_STEP]
    for instance_id in reviewed:
        try:
            instance = get_session_instance(
                case, pair, instance_id, include_statements=False
            )
            if instance["summary"] is None:
                _, created = queue_summary(
                    batch.requested_by,
                    case,
                    instance,
                    backend=backend,
                    submission_id=uuid5(batch.pk, instance_id),
                    execution=scope["execution"],
                    batch=batch,
                )
                batch.queued_runs += int(created)
        except AnalysisError, ValidationError, Http404:
            batch.unavailable_sources += 1
        batch.processed_sources += 1
    return batch.processed_sources < len(scope["instances"])
