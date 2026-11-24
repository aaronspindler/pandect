"""Bounded summary requests pinned to ordinal ranges of a published session."""

from uuid import UUID, uuid5

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import Case, F, Q, TextField, When
from django.db.models.fields.json import KT
from django.db.models.functions import Cast, Length
from django.http import Http404
from django.urls import reverse

from core.analysis.errors import AnalysisError
from core.analysis.manual import start_available_run
from core.analysis.operations import get_operation
from core.analysis.presentation import participant_text
from core.models import AnalysisRun
from phone.analysis.ai import SUMMARY_CHUNK_CHARACTERS
from phone.saved_sessions import valid_sessions

OPERATION = "session_summaries"
RANGE_KEY = "_saved_session_range"


def summary_members(session, case):
    """Fetch at most one range's character budget per original."""
    return (
        session.members.filter(case=case)
        .alias(characters=Length("message__body"))
        .annotate(
            summary_text=Case(
                When(characters__lte=SUMMARY_CHUNK_CHARACTERS, then="message__body"),
                output_field=TextField(),
            )
        )
        .order_by("ordinal")
        .values_list("ordinal", "message_id", "summary_text", "message__is_archived")
    )


def summary_gap(body, archived):
    if archived:
        return "Archived"
    if body is None:
        return "Message exceeds the summary limit"
    if not body.strip():
        return "No text to summarize"
    return ""


def checked_session_range(case, run, sources):
    scope = run.parameters.get(RANGE_KEY)
    if not isinstance(scope, dict):
        raise AnalysisError("source_changed")
    try:
        session_id = UUID(scope["session"])
        first, last = scope["first"], scope["last"]
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise AnalysisError("source_changed") from exc
    if (
        type(first) is not int
        or type(last) is not int
        or first < 1
        or last - first + 1 != len(sources)
        or not sources
        or len(sources) > settings.PANDECT_ANALYSIS_MAX_SOURCES
    ):
        raise AnalysisError("source_changed")
    session = (
        valid_sessions(case)
        .filter(pk=session_id, message_count__gte=last)
        .select_related("grouping")
        .first()
    )
    if session is None:
        raise AnalysisError("source_unavailable")
    members = (
        session.members.filter(
            case=case,
            ordinal__range=(first, last),
        )
        .order_by("ordinal")
        .values_list("message_id", "sha256")
    )
    if any(
        s.kind != "text-message" or s.derived_result_id is not None for s in sources
    ) or list(members) != [(s.item.pk, s.item.sha256) for s in sources]:
        raise AnalysisError("source_changed")
    return session


def range_instance_url(case, run, sources):
    from phone.conversation_instances import instance_url

    try:
        session = checked_session_range(case, run, sources)
    except AnalysisError:
        return ""
    group = session.grouping
    return instance_url(
        case, (group.first_person_id, group.second_person_id), session.pk
    )


def session_reports(case, session):
    group = session.grouping
    aliases = (
        valid_sessions(case)
        .filter(
            grouping__first_person_id=group.first_person_id,
            grouping__second_person_id=group.second_person_id,
            membership_digest=session.membership_digest,
            message_count=session.message_count,
        )
        .annotate(key=Cast("pk", output_field=TextField()))
        .order_by()
        .values("key")
    )
    return AnalysisRun.objects.alias(
        session_key=KT("parameters___saved_session_range__session")
    ).filter(
        case=case,
        operation=OPERATION,
        session_key__in=aliases,
    )


def _reserved_ordinals(case, session, first, last, executions, grant):
    """Reserve individual originals across overlapping completed or pending runs."""
    reports = (
        session_reports(case, session)
        .filter(
            Q(
                status="complete",
                result__case=case,
                result__data__metrics__range_covered=True,
            )
            | Q(
                status__in=("running", "queued"),
                method_version=get_operation(OPERATION).version,
            ),
            parameters___saved_session_range__first__lte=last,
            parameters___saved_session_range__last__gte=first,
        )
        .values_list("status", "parameters")
    )
    reserved = {key: set() for key in executions}
    for status, parameters in reports.iterator(chunk_size=50):
        route = parameters.get("backend")
        if route not in executions or parameters.get("_execution") != executions[route]:
            continue
        if (
            status != "complete"
            and route == "gateway"
            and parameters.get("_external_approval", {}).get("revision") != grant
        ):
            continue
        scope = parameters[RANGE_KEY]
        reserved[route].update(
            range(max(first, scope["first"]), min(last, scope["last"]) + 1)
        )
    return reserved


def prepare_summary_range(batch, case, instance, executions):
    """Prepare one range and commit its cursor in the batch's existing transaction."""
    session = instance["saved_session"]
    cursor = batch.scope.get("_cursor", {})
    first = cursor["next_ordinal"] if cursor.get("session") == str(session.pk) else 1
    limit = min(
        session.message_count, first + settings.PANDECT_ANALYSIS_MAX_SOURCES - 1
    )
    reserved = _reserved_ordinals(
        case,
        session,
        first,
        limit,
        executions,
        batch.scope.get("external_grant_revision"),
    )
    rows = summary_members(session, case).filter(ordinal__range=(first, limit))
    selected = []
    size = 0
    last = first - 1
    routes = set()
    for ordinal, message_id, body, archived in rows.iterator(chunk_size=16):
        if ordinal != last + 1:
            raise AnalysisError("source_changed")
        needed = {route for route in executions if ordinal not in reserved[route]}
        gap = summary_gap(body, archived)
        characters = len(body) if body is not None else 0
        # Each outbound call covers a contiguous, still missing range on its own route.
        if selected and (
            needed != routes or gap or size + characters > SUMMARY_CHUNK_CHARACTERS
        ):
            break
        last = ordinal
        if not needed:
            continue
        if gap:
            batch.unavailable_sources += 1
            continue
        if not selected:
            first = ordinal
            routes = needed
        selected.append({"kind": "text-message", "id": str(message_id)})
        size += characters
    if last < first:
        raise AnalysisError("source_changed")
    scope = {"session": str(session.pk), "first": first, "last": last}
    for backend in sorted(routes):
        try:
            _, created = start_available_run(
                batch.requested_by,
                case,
                OPERATION,
                selected,
                {"backend": backend, RANGE_KEY: scope},
                executions[backend],
                submission_id=uuid5(batch.pk, f"{session.pk}:{first}:{last}:{backend}"),
                batch=batch,
            )
            batch.queued_runs += int(created)
        except AnalysisError, ValidationError, Http404:
            batch.unavailable_sources += 1
    if last == session.message_count:
        batch.scope.pop("_cursor", None)
        return True
    batch.scope["_cursor"] = {"session": str(session.pk), "next_ordinal": last + 1}
    return False


def coverage_url(case, session):
    group = session.grouping
    return reverse(
        "phone:session_summary_coverage",
        args=(case.pk, group.first_person_id, group.second_person_id, session.pk),
    )


def attach_range_summary(case, instance, executions, include_statements):
    """Count the union of completed ranges and attach one attributable preview."""
    session = instance["saved_session"]
    # Ordered intervals allow coverage to be counted even across retries or a
    # new range size, without pulling original IDs or entire reports into RAM.
    spans = {key: [0, 0] for key in (None, *executions)}
    reports = (
        session_reports(case, session)
        .filter(result__case=case, status__in=("complete", "partial"))
        .annotate(
            first_ordinal=F("parameters___saved_session_range__first"),
            last_ordinal=F("parameters___saved_session_range__last"),
            covered=F("result__data__metrics__range_covered"),
        )
        .order_by("first_ordinal", "-created_at", "pk")
    )
    cols = [
        "first_ordinal",
        "last_ordinal",
        "covered",
        "status",
        "parameters___execution",
    ]
    if include_statements:
        cols.append("result__data__items__0__text")
    preview = ""
    available = False
    for first, last, covered, status, execution, *body in reports.values_list(
        *cols
    ).iterator(chunk_size=50):
        if (
            type(first) is not int
            or type(last) is not int
            or not 1 <= first <= last <= session.message_count
        ):
            continue
        available = True
        if not preview and body and isinstance(body[0], str):
            people = (session.grouping.first_person, session.grouping.second_person)
            names = {
                (role, str(person.pk)): str(person)
                for person in people
                for role in ("sender", "recipient")
            }
            preview = participant_text(body[0], names, operation=OPERATION)
        if covered is not True or status != "complete":
            continue
        for route, span in spans.items():
            if route is None or executions[route] == execution:
                end, count = span
                span[:] = [
                    max(end, last),
                    count + max(0, last - max(first - 1, end)),
                ]
    count = spans[None][1]
    instance["summary_routes"].update(
        route for route in executions if spans[route][1] == session.message_count
    )
    if available and (not instance["summary"] or count == session.message_count):
        instance["summary"] = {
            "partial": count != session.message_count,
            "preview": preview,
            "report_url": instance["coverage_url"],
            "coverage_label": (f"{count} of {session.message_count} messages covered"),
        }
