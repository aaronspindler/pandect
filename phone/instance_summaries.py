"""Review and prepare bounded summaries for exact conversation instances."""

from hashlib import sha256
from uuid import UUID, uuid5

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.db.models.functions import Length
from django.http import Http404

from core.analysis.bulk import start_case_analysis
from core.analysis.errors import AnalysisError
from core.analysis.execution import (
    available_executions,
    case_execution_grants,
    case_grant_revision,
    execution_confirmation,
)
from core.analysis.manual import start_available_run
from core.analysis.operations import OPERATIONS
from core.models import AnalysisRun, CaseAnalysisBatch
from people.models import Person
from phone.analysis.ai import summary_chunks
from phone.analysis.deterministic.conversations import DEFAULT_SESSION_GAP_MINUTES
from phone.conversation_instances import get_session_instance
from phone.conversations import pair_messages
from phone.saved_summary_ranges import (
    attach_range_summary,
    coverage_url,
    prepare_summary_range,
)

OPERATION = "session_summaries"
INSTANCES_PER_STEP = 10


def summary_review_revision(case, ids, executions):
    grant = (
        case_grant_revision(case, OPERATION, executions.get("gateway"))
        if "gateway" in executions
        else None
    )
    return execution_confirmation(
        "phone.instance_summary", [str(case.pk), ids, grant], executions
    )


def request_summaries(
    actor, case, people, ids, executions, submission_id, *, single=False
):
    """Queue the exact review scope while the caller holds the case lock."""
    return start_case_analysis(
        actor,
        case.pk,
        submission_id=submission_id,
        scope={
            "type": "instance_summaries",
            "people": people,
            "instances": ids,
            "single": single,
            "executions": executions,
            "reviewed_scope": summary_review_revision(case, ids, executions),
            "external_grant_revision": case_grant_revision(
                case, OPERATION, executions.get("gateway")
            ),
        },
    )


def active_summary_batch(case, people, executions, instance_id=None):
    if not executions:
        return None
    backend, execution = next(iter(executions.items()))
    batches = CaseAnalysisBatch.objects.filter(
        case=case, scope__type="instance_summaries", scope__people=people
    )
    matching = Q(**{f"scope__executions__{backend}": execution})
    if backend == "gateway":
        matching &= Q(
            scope__external_grant_revision__in=list(
                case_execution_grants(case, OPERATION, execution)
            )
        )
    else:
        matching |= Q(scope__backend="local", scope__execution=execution)
    batches = batches.filter(matching)
    if instance_id is None:
        batches = batches.filter(
            Q(scope__single=False) | ~Q(scope__has_key="single"),
        ).filter(
            Q(status=CaseAnalysisBatch.Status.QUEUED)
            | Q(
                runs__parameters___execution=execution,
                runs__status__in=(
                    AnalysisRun.Status.QUEUED,
                    AnalysisRun.Status.RUNNING,
                ),
            )
        )
    else:
        batches = batches.filter(scope__instances__contains=[str(instance_id)]).filter(
            Q(status=CaseAnalysisBatch.Status.QUEUED)
            | Q(
                runs__parameters___saved_session_range__session=str(instance_id),
                runs__parameters___execution=execution,
                runs__status__in=(
                    AnalysisRun.Status.QUEUED,
                    AnalysisRun.Status.RUNNING,
                ),
            )
        )
    return batches.only("case_id").first()


def submitted_summary_batch(
    actor, case, people, instance_id, *, submission_id, reviewed_scope
):
    try:
        identifier = UUID(submission_id)
    except ValueError:
        return None
    batches = CaseAnalysisBatch.objects.filter(
        submission_id=identifier,
        case=case,
        requested_by=actor,
        scope__type="instance_summaries",
        scope__people=people,
    )
    if instance_id is None:
        batch = batches.filter(
            Q(scope__single=False) | ~Q(scope__has_key="single")
        ).first()
        if batch is None:
            return None
        expected_scope = batch.scope.get("reviewed_scope")
        if expected_scope is None:
            expected_scope = sha256(
                "|".join(batch.scope["instances"]).encode()
            ).hexdigest()
        if reviewed_scope == expected_scope:
            return batch
        return None
    return batches.filter(
        scope__single=True, scope__instances=[str(instance_id)]
    ).first()


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


def pending_summaries(case, instances, executions):
    """Find in-flight routes with the same originals and grouping settings."""
    instances = [row for row in instances if not row.get("saved_session")]
    if not instances or not executions:
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
    grants = case_execution_grants(case, OPERATION, executions.get("gateway"))
    for run in (
        AnalysisRun.objects.filter(
            candidates,
            case=case,
            operation=OPERATION,
            method_version=OPERATIONS[OPERATION].version,
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
        backend = run.parameters.get("backend")
        if (
            backend in executions
            and run.parameters.get("_execution") == executions[backend]
            and (
                backend != "gateway"
                or run.parameters.get("_external_approval", {}).get("revision")
                in grants
            )
            and grouping is not None
            and grouping == _grouping_key(run.parameters)
        ):
            pending.setdefault(instance_id, {}).setdefault(backend, run)
    return pending


def review_summary_candidates(case, pair, page, executions):
    """Count pending and oversized instances and return the requestable ones."""
    missing = [instance for instance in page if instance["summary_missing"]]
    pending = pending_summaries(case, missing, executions)
    unrequested = [
        instance
        for instance in missing
        if executions.keys()
        - instance["summary_routes"]
        - pending.get(instance["id"], {}).keys()
    ]
    originals = summary_originals(
        pair,
        {
            pk
            for instance in unrequested
            if not instance.get("saved_session")
            for pk in instance["member_ids"]
        },
    )
    eligible = []
    oversized = 0
    for instance in unrequested:
        if not instance.get("saved_session") and summary_limit(
            instance["member_ids"], originals
        ):
            oversized += 1
        else:
            eligible.append(instance)
    return eligible, oversized, len(pending)


def queue_summary(
    actor, case, instance, *, backend, submission_id, execution, batch=None
):
    return start_available_run(
        actor,
        case,
        OPERATION,
        [{"kind": "text-message", "id": str(pk)} for pk in instance["member_ids"]],
        _summary_parameters(instance) | {"backend": backend},
        execution,
        submission_id=submission_id,
        batch=batch,
        external_grant_revision=batch.scope.get("external_grant_revision")
        if batch
        else None,
    )


def prepare_instance_summaries(batch, case):
    """Queue one run per reviewed instance, without merging overlapping groups."""
    scope = batch.scope
    executions = scope.get("executions")
    if executions is None:
        # Work queued before standing approval retains its original private route.
        executions = {scope["backend"]: scope["execution"]}
    available = available_executions(case, OPERATION, {})
    grants = case_execution_grants(case, OPERATION, executions.get("gateway"))
    eligible = {
        backend: execution
        for backend, execution in executions.items()
        if available.get(backend) == execution
        and (backend != "gateway" or scope.get("external_grant_revision") in grants)
    }
    if not eligible:
        raise AnalysisError("execution_changed")
    people = [Person.objects.filter(case=case, pk=pk).first() for pk in scope["people"]]
    if None in people:
        raise AnalysisError("source_unavailable")
    pair = pair_messages(case, *people)
    start = batch.processed_sources
    reviewed = scope["instances"][start : start + INSTANCES_PER_STEP]
    for instance_id in reviewed:
        try:
            instance = get_session_instance(
                case, pair, instance_id, include_statements=False, executions=eligible
            )
            if instance.get("saved_session"):
                missing = {
                    key: value
                    for key, value in eligible.items()
                    if key not in instance["summary_routes"]
                }
                if missing and not prepare_summary_range(
                    batch, case, instance, missing
                ):
                    return True
                batch.processed_sources += 1
                continue
            pending = pending_summaries(case, [instance], eligible).get(
                instance["id"], {}
            )
            for backend, execution in eligible.items():
                if backend in instance["summary_routes"] or backend in pending:
                    continue
                _, created = queue_summary(
                    batch.requested_by,
                    case,
                    instance,
                    backend=backend,
                    submission_id=uuid5(
                        batch.pk,
                        f"{instance_id}:{backend}"
                        if "executions" in scope
                        else instance_id,
                    ),
                    execution=execution,
                    batch=batch,
                )
                batch.queued_runs += int(created)
            if eligible.keys() != executions.keys():
                batch.unavailable_sources += 1
        except AnalysisError, ValidationError, Http404:
            batch.unavailable_sources += 1
        batch.processed_sources += 1
    return batch.processed_sources < len(scope["instances"])


def attach_saved_summaries(
    case,
    instances,
    *,
    full_summary_id=None,
    include_statements=True,
    preview_only=False,
    executions=None,
):
    """Attach reports whose verified ordinal ranges cover this saved membership."""
    from phone.conversation_instances import attach_instance_summaries

    instances = list(instances)
    if not instances:
        return
    executions = executions or {}
    legacy = []
    reports = AnalysisRun.objects.filter(
        case=case, operation=OPERATION, result__case=case
    )
    has_legacy = reports.exclude(parameters__has_key="_saved_session_range").exists()
    has_ranges = reports.filter(parameters__has_key="_saved_session_range").exists()
    for instance in instances:
        instance["summary"] = None
        instance["summary_routes"] = set()
        if has_legacy and instance["count"] <= settings.PANDECT_ANALYSIS_MAX_SOURCES:
            ids = tuple(
                instance["saved_session"]
                .members.filter(case=case)
                .order_by("ordinal")
                .values_list("message_id", flat=True)
            )
            legacy.append({"id": instance["id"], "member_ids": ids, "summary": None})
    if legacy:
        attach_instance_summaries(
            case,
            legacy,
            full_summary_id=full_summary_id,
            include_statements=include_statements,
            preview_only=preview_only,
            executions=executions,
        )
    historical = {row["id"]: row for row in legacy}
    for instance in instances:
        if previous := historical.get(instance["id"]):
            instance["summary"] = previous["summary"]
            instance["summary_routes"].update(previous["summary_routes"])
        session = instance["saved_session"]
        instance["coverage_url"] = coverage_url(case, session)
        if has_ranges:
            attach_range_summary(case, instance, executions, include_statements)
        instance["summary_missing"] = (
            not instance["summary"]
            or not instance["summary_routes"] >= executions.keys()
        )
