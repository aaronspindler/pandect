"""Owner-selected, resumable background preparation of searchable summaries."""

from uuid import UUID, uuid4, uuid5

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Sum
from django.http import Http404

from cases.models import Case
from core.analysis.bulk import start_case_analysis
from core.analysis.errors import AnalysisError
from core.analysis.execution import (
    case_grant_revision,
    execution_confirmation,
    execution_destination,
    selected_ai_executions,
)
from core.analysis.manual import start_available_run
from core.analysis.operations import get_operation
from core.models import AnalysisRun, CaseAnalysisBatch
from phone.saved_sessions import distinct_sessions, saved_row

from .generation_briefs import (
    BRIEF_SOURCES,
    SECTION_CHARS,
    brief_estimate,
    eligible_sections,
    section,
)
from .parents import active_sessions

OPERATIONS = {"conversations": "session_summaries", "briefs": "text_topics"}
LABELS = {
    "conversations": "Conversation summaries",
    "briefs": "Document and transcript briefs",
}


def route(case, feature):
    op = OPERATIONS[feature]
    execution = selected_ai_executions(case, op, {}).get(case.ai_backend)
    if execution is None:
        return {}
    return {"execution": execution, "grant": case_grant_revision(case, op, execution)}


def approved_policy(case, feature, revision):
    entry = case.search_generation_policy.get(feature, {})
    if (
        feature not in OPERATIONS
        or not revision
        or entry.get("revision") != revision
        or entry.get("owner") != str(case.owner_id)
        or not entry.get("route")
        or entry["route"] != route(case, feature)
    ):
        raise AnalysisError("execution_changed")
    return entry


def _sessions(case):
    return distinct_sessions(active_sessions(case))


def settings_rows(case, actor, *, estimate=False):
    rows = []
    for feature in OPERATIONS:
        saved = case.search_generation_policy.get(feature, {})
        destination = route(case, feature)
        enabled = bool(saved.get("route"))
        row = {
            "feature": feature,
            "label": LABELS[feature],
            "enabled": enabled,
            "ready": bool(destination),
            "current": enabled and destination == saved["route"],
            "destination": execution_destination(destination["execution"])
            if destination
            else "",
            "model": destination["execution"]["model"] if destination else "",
            "confirmation": execution_confirmation(
                "case.search.generation",
                [str(case.pk), str(actor.pk), feature, saved.get("revision"), enabled],
                destination,
            ),
        }
        if estimate:
            if feature == "conversations":
                row.update(
                    _sessions(case).aggregate(
                        estimate=Count("pk"), units=Sum("message_count", default=0)
                    )
                )
            else:
                row.update(brief_estimate(case))
        rows.append(row)
    return rows


def set_policy(case, feature, enable):
    entry = {"revision": str(uuid4())}
    if enable:
        target = route(case, feature)
        if not target:
            raise AnalysisError("execution_changed")
        entry.update(owner=str(case.owner_id), route=target)
    case.search_generation_policy = case.search_generation_policy | {feature: entry}
    case.save(update_fields=("search_generation_policy",))
    if enable:
        enqueue_generation(case, feature)


def enqueue_generation(case, feature, *, target="", pk=None):
    """Register background discovery in the publishing transaction."""
    saved = case.search_generation_policy.get(feature, {})
    if not saved.get("route"):
        return
    revision = saved["revision"]
    if pk is None:
        pending = CaseAnalysisBatch.objects.filter(
            case=case,
            status="queued",
            scope__type="search_generation",
            scope__feature=feature,
            scope__revision=revision,
            scope__target="",
        ).first()
        if pending:
            return pending, False
    key = (
        uuid5(UUID(revision), f"{target}:{pk}")
        if pk and not target.startswith("restored:")
        else uuid4()
    )
    scope = {
        "type": "search_generation",
        "feature": feature,
        "revision": revision,
        "target": target,
        "pk": str(pk) if pk else None,
        "external_grant_revision": saved["route"]["grant"],
    }
    return start_case_analysis(case.owner, case.pk, submission_id=key, scope=scope)


@transaction.atomic
def published(case_id, feature, target, pk):
    case = (
        Case.objects.select_for_update(of=("self",))
        .filter(
            pk=case_id,
            owner__is_active=True,
            **{f"search_generation_policy__{feature}__has_key": "route"},
        )
        .first()
    )
    if case is not None:
        enqueue_generation(case, feature, target=target, pk=pk)


def _next(qs, batch):
    if batch.source_cursor:
        qs = qs.filter(pk__gt=batch.source_cursor)
    return qs.order_by("pk").first()


def _conversation_step(batch, case, entry):
    from phone.instance_summaries import attach_saved_summaries
    from phone.saved_summary_ranges import prepare_summary_range

    sessions = _sessions(case)
    if batch.scope["target"] == "grouping":
        sessions = sessions.filter(grouping_id=batch.scope["pk"])
    elif batch.scope["target"] == "restored:text-message":
        sessions = sessions.filter(
            members__message_id=batch.scope["pk"], members__case=case
        )
    elif batch.scope["target"] == "restored:document":
        sessions = sessions.filter(
            members__case=case,
            members__message__communication_context__source_document_id=batch.scope[
                "pk"
            ],
        )
    session = _next(sessions.select_related("grouping"), batch)
    if session is None:
        return False
    execution = entry["route"]["execution"]
    routes = {execution["backend"]: execution}
    instance = saved_row(session)
    attach_saved_summaries(
        case, [instance], include_statements=False, executions=routes
    )
    if instance["summary_missing"] and not prepare_summary_range(
        batch, case, instance, routes
    ):
        return True
    batch.source_cursor = session.pk
    batch.processed_sources += 1
    return True


def _brief_step(batch, case, entry):
    """Prepare at most one section per delivery; skip previously processed sections."""
    feature = batch.scope["feature"]
    for kind in BRIEF_SOURCES:
        if batch.source_kind and BRIEF_SOURCES.index(kind) < BRIEF_SOURCES.index(
            batch.source_kind
        ):
            continue
        target = batch.scope["target"]
        qs = eligible_sections(case, kind)
        if target.startswith("restored:"):
            original_kind = target.removeprefix("restored:")
            pk = batch.scope["pk"]
            if kind == "text" and original_kind == "text":
                qs = qs.filter(pk=pk)
            elif kind == "attachment" and original_kind in {"facetime", "phone-call"}:
                parent = (
                    "facetime_id" if original_kind == "facetime" else "phone_call_id"
                )
                qs = qs.filter(**{parent: pk})
            elif kind == "extraction":
                qs = qs.filter(
                    result__run__inputs__contains=[
                        {"kind": original_kind, "evidence_id": pk}
                    ]
                )
            else:
                continue
        elif target:
            if target != ("result" if kind == "extraction" else kind):
                continue
            qs = qs.filter(
                **{"result_id" if kind == "extraction" else "pk": batch.scope["pk"]}
            )
        if batch.source_kind != kind:
            batch.source_cursor = None
            batch.source_kind = kind
        candidate = _next(qs.only("pk"), batch)
        if candidate is None:
            continue
        cursor = batch.scope.get("_cursor", {})
        start = cursor["start"] if cursor.get("id") == str(candidate.pk) else 0
        try:
            item, selection, version = section(case, kind, candidate.pk, start)
            ref = {"type": kind, "id": str(item.pk), "start": start, "version": version}
            matches = AnalysisRun.objects.filter(
                case=case,
                operation=OPERATIONS[feature],
                method_version=get_operation(OPERATIONS[feature]).version,
                parameters___brief_section=ref,
            )
            current = matches.filter(
                status__in=("complete", "empty"), result__case=case
            ).exists()
            pending = matches.filter(
                status__in=("queued", "running"),
                parameters___generation_revision=entry["revision"],
            ).exists()
            if item.body_window.strip() and not current and not pending:
                execution = entry["route"]["execution"]
                _, created = start_available_run(
                    batch.requested_by,
                    case,
                    OPERATIONS[feature],
                    [selection],
                    {
                        "backend": execution["backend"],
                        "include_summary": True,
                        "_suggestions_only": True,
                        "_search_generation": feature,
                        "_generation_revision": entry["revision"],
                        "_brief_section": ref,
                    },
                    execution,
                    external_grant_revision=entry["route"]["grant"],
                    submission_id=uuid5(
                        batch.pk, f"{kind}:{item.pk}:{start}:{version}"
                    ),
                    batch=batch,
                )
                batch.queued_runs += int(created)
            if start + SECTION_CHARS < item.body_length:
                batch.scope["_cursor"] = {
                    "id": str(item.pk),
                    "start": start + SECTION_CHARS,
                }
                return True
        except AnalysisError, ValidationError, Http404:
            batch.unavailable_sources += 1
        batch.scope.pop("_cursor", None)
        batch.source_cursor = candidate.pk
        batch.processed_sources += 1
        return True
    return False


def prepare_step(batch, case):
    feature = batch.scope["feature"]
    entry = approved_policy(case, feature, batch.scope["revision"])
    if batch.requested_by_id != case.owner_id:
        raise AnalysisError("access_removed")
    if feature == "conversations":
        return _conversation_step(batch, case, entry)
    return _brief_step(batch, case, entry)
