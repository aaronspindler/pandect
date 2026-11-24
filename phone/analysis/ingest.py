"""Automatic, bounded analysis of newly published CSV messages."""

from collections import defaultdict
from datetime import timedelta
from uuid import uuid5

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import F, Q
from django.http import Http404

from core.analysis.bulk import retained_cursor_time, start_case_analysis
from core.analysis.errors import AnalysisError, error_message
from core.analysis.execution import requires_inference, snapshot_execution
from core.analysis.gateway import gateway_configured
from core.analysis.history import (
    communication_selection,
    comparison_parameters,
    wording_selection,
)
from core.analysis.import_checks import imported_sources
from core.analysis.services import start_run
from core.analysis.sources import source_id
from core.evidence.queries import scoped_evidence

MODEL_PARAMETERS = {
    "text_topics": {"include_summary": True},
    "content_events": {},
    "session_summaries": {"gap_minutes": 60},
    "conversation_link": {"semantic_links": True},
}


def import_analysis_executions():
    """Snapshot reviewable message-analysis routes without credentials."""
    backend = (
        "gateway"
        if all(gateway_configured("text", operation) for operation in MODEL_PARAMETERS)
        else "local"
    )
    return {
        f"{operation}:{backend}": snapshot_execution(
            operation, parameters | {"backend": backend}
        )
        for operation, parameters in MODEL_PARAMETERS.items()
    }


def import_analysis_scope(run):
    """Keep publication, progress and deliberate reruns on the same settings."""
    from core.analysis.reprocessing import analysis_scope

    return {"type": "messages", "id": str(run.pk), "automatic": True} | (
        run.parameters["analysis"]
        if "analysis" in run.parameters
        else analysis_scope(run.case, ["text-message"])
    )


def _analysis_backend(scope):
    mode = scope.get("mode", "local")
    if mode not in {"local", "ai", "both"}:
        raise AnalysisError("execution_changed")
    expected = {} if mode == "local" else import_analysis_executions()
    if scope.get("executions", {}) != expected:
        raise AnalysisError("execution_changed")
    return next(iter(expected.values()))["backend"] if expected else ""


def start_import_analysis(run, data):
    """Commit the preparation task with publication; retries keep one batch."""
    if data["metrics"].get("created_rows"):
        scope = import_analysis_scope(run)
        if scope.get("analysis_version"):
            scope["source_count"] = data["metrics"]["created_rows"]
        start_case_analysis(
            run.requested_by,
            run.case_id,
            submission_id=uuid5(run.pk, "imessage-analysis-v1"),
            scope=scope,
        )


def next_messages(batch, case):
    """Resume in reported-time order, with unknown times last and stable ties."""
    _, messages = imported_sources(case, batch.scope)
    messages = messages.filter(created_at__lte=batch.created_at)
    if batch.source_cursor:
        if "_cursor" in batch.scope:
            moment = retained_cursor_time(batch)
        else:
            moment = messages.get(pk=batch.source_cursor).occurred_at
        if moment is None:
            messages = messages.filter(
                occurred_at__isnull=True, pk__gt=batch.source_cursor
            )
        else:
            messages = messages.filter(
                Q(occurred_at__gt=moment)
                | Q(occurred_at=moment, pk__gt=batch.source_cursor)
                | Q(occurred_at__isnull=True)
            )
    if batch.scope.get("analysis_version"):
        messages = messages.filter(is_archived=False)
    messages = messages.order_by(F("occurred_at").asc(nulls_last=True), "pk")
    # Leave room for same-participant call metadata in cross-channel comparisons.
    limit = min(
        25 if batch.scope.get("analysis_version") else 250,
        settings.PANDECT_ANALYSIS_MAX_SOURCES,
    )
    selected, characters = [], 0
    for message in messages[:limit].iterator(chunk_size=10):
        length = len(message.body)
        if selected and characters + length > 1_000_000:
            break
        selected.append(message)
        characters += length
    return selected


def _groups(messages, *, count, characters):
    """Cover every message, keeping oversized originals intact for visible failure."""
    group, size = [], 0
    for message in messages:
        if group and (len(group) >= count or size + len(message.body) > characters):
            yield group
            group, size = [], 0
        group.append(message)
        size += len(message.body)
    if group:
        yield group


def prepare_import_analysis(batch, case, messages):
    """Queue independent operations; model/provider failures cannot undo ingestion."""
    from core.analysis.reprocessing import ANALYSIS_VERSION

    versioned = batch.scope.get("analysis_version") == ANALYSIS_VERSION
    backend = "" if versioned else _analysis_backend(batch.scope)
    if backend == "gateway" and (
        not batch.scope.get("external_processing")
        or batch.scope.get("approved_by") != str(batch.requested_by_id)
    ):
        raise AnalysisError("retry_approval_required")
    executions = batch.scope.get("executions", {})
    total = batch.scope.get("source_count")
    if total is None:
        _, imported = imported_sources(case, batch.scope)
        total = imported.count()
    limit = settings.PANDECT_ANALYSIS_MAX_SOURCES
    compared = {}

    def queue(operation, selected, parameters=None, *, additions=(), matching_calls=0):
        parameters = parameters or {}
        choices = [{"kind": "text-message", "id": str(item.pk)} for item in selected]
        if versioned and operation == "copied_text":
            peers = wording_selection(
                case, batch, choices, [item.body for item in selected]
            )
            parameters = comparison_parameters(choices, peers, parameters)
            if len(peers) < 2:
                return
            choices = peers
        if versioned and operation in {
            "sessions",
            "comm_patterns",
            "session_summaries",
            "conversation_link",
        }:
            exchange = operation == "conversation_link"
            model = operation == "session_summaries" or parameters.get(
                "semantic_links", False
            )
            key = (tuple(item.pk for item in selected), exchange, model)
            if key not in compared:
                compared[key] = communication_selection(
                    case,
                    batch,
                    choices,
                    selected,
                    minutes=10 if exchange else 60,
                    include_calls=exchange,
                    session_replies=not exchange,
                    count=20 if model else None,
                    characters=16_000 if model else 1_000_000,
                    anchor_chars=sum(len(item.body) for item in selected),
                )
            peers = compared[key]
            if exchange:
                parameters = parameters | {"time_window_minutes": 10}
            parameters = comparison_parameters(choices, peers, parameters)
            choices = peers
        choices = [*choices, *additions]
        scope = {
            "import_run_id": batch.scope["id"],
            "imported_messages": total,
            "selected_messages": len(selected),
            "additional_calls": len(additions),
            "matching_calls": matching_calls,
        }
        route = (
            executions.get(f"{operation}:{parameters.get('backend', 'local')}")
            if requires_inference(operation, parameters)
            else None
        )
        try:
            _, created = start_run(
                batch.requested_by,
                case.pk,
                operation,
                choices,
                parameters=parameters | {"_import_analysis": scope},
                submission_id=uuid5(
                    batch.pk, f"{operation}:{selected[0].pk}:{parameters}"
                ),
                batch=batch,
                approved_execution=route,
                external_grant_revision=batch.scope.get("external_grant_revision"),
            )
        except (AnalysisError, ValidationError, Http404) as failure:
            if not versioned and not (route and route["location"] == "external"):
                raise
            batch.unavailable_sources += 1
            if len(batch.preparation_errors) < 100:
                code = (
                    failure.code
                    if isinstance(failure, AnalysisError)
                    else "source_unavailable"
                )
                batch.preparation_errors.append(
                    {
                        "kind": "text-message",
                        "evidence_id": str(selected[0].pk),
                        "operation": operation,
                        "message": error_message(code),
                    }
                )
        else:
            batch.queued_runs += created

    if batch.scope.get("mode", "local") != "ai":
        queue("entities", messages)
        queue("text_topics", messages, {"include_summary": False})
        if versioned:
            from phone.grouping_jobs import register_message_pairs

            register_message_pairs(batch, case, messages)
        else:
            queue("sessions", messages, {"gap_minutes": 60})
        queue("comm_patterns", messages, {"gap_minutes": 60})

        calls, matching_calls = [], 0
        timed = [message for message in messages if message.occurred_at is not None]
        if timed and not versioned:
            first = messages[0]
            pair = Q(sender_id=first.sender_id, recipient_id=first.recipient_id) | Q(
                sender_id=first.recipient_id, recipient_id=first.sender_id
            )
            for kind in ("phone-call", "facetime"):
                available = scoped_evidence(case, kind).filter(
                    pair,
                    is_archived=False,
                    created_at__lte=batch.created_at,
                    occurred_at__gte=timed[0].occurred_at - timedelta(minutes=10),
                    occurred_at__lte=timed[-1].occurred_at + timedelta(minutes=10),
                )
                matching_calls += available.count()
                calls.extend(
                    {"kind": kind, "id": str(pk)}
                    for pk in available.order_by("occurred_at", "pk").values_list(
                        "pk", flat=True
                    )[: max(0, limit - len(messages) - len(calls))]
                )
        queue(
            "conversation_link",
            messages,
            {"time_window_minutes": 10},
            additions=calls,
            matching_calls=matching_calls,
        )

        by_sender = defaultdict(list)
        for message in messages:
            by_sender[message.sender_id].append(message)
        for selected in by_sender.values():
            queue("style_profiles", selected)
            timed_samples = [item for item in selected if item.occurred_at is not None]
            times = sorted({item.occurred_at for item in timed_samples})
            if len(times) < 2:
                continue
            midpoint = timed_samples[len(timed_samples) // 2].occurred_at
            if midpoint == times[0]:
                midpoint = times[1]
            earlier = [item for item in timed_samples if item.occurred_at < midpoint]
            later = [item for item in timed_samples if item.occurred_at >= midpoint]
            if earlier and later:
                queue(
                    "style_change",
                    timed_samples,
                    {
                        "group_a": [
                            source_id({"kind": "text-message", "id": str(item.pk)})
                            for item in earlier
                        ],
                        "group_b": [
                            source_id({"kind": "text-message", "id": str(item.pk)})
                            for item in later
                        ],
                    },
                )

        for group in _groups(messages, count=min(50, limit), characters=100_000):
            queue("copied_text", group)
    if backend or versioned:
        for group in _groups(messages, count=min(20, limit), characters=16_000):
            for operation, parameters in MODEL_PARAMETERS.items():
                for route in ("local", "gateway") if versioned else (backend,):
                    if f"{operation}:{route}" in executions:
                        queue(
                            operation,
                            group,
                            parameters
                            | {"backend": route}
                            | (
                                {"_suggestions_only": True}
                                if versioned
                                and operation in {"text_topics", "conversation_link"}
                                else {}
                            ),
                        )


def import_selection_warning(selected, imported):
    return (
        f"Automatic analysis of {selected} of {imported} newly imported messages. "
        "Each run covers only its retained selection. Comparisons do not span "
        "separate selections; a selection boundary can cut an exchange."
    )


def describe_import_selection(data, run):
    """Disclose bounded coverage rather than implying whole-conversation analysis."""
    scope = run.parameters.get("_import_analysis")
    if scope is None:
        return
    data["metrics"]["import_selection"] = scope
    data["warnings"].append(
        import_selection_warning(scope["selected_messages"], scope["imported_messages"])
    )
    if run.operation in {"style_profiles", "style_change"}:
        data["warnings"].append(
            "Writing samples group the reported sender. Comparisons split that "
            "sender's timed samples at a distinct timestamp near the middle."
        )
    if (
        run.operation == "conversation_link"
        and not run.parameters.get("semantic_links")
        and "_comparison" not in run.parameters
    ):
        data["warnings"].append(
            f"Selected {scope['additional_calls']} of {scope['matching_calls']} "
            "matching same-participant calls within ten minutes of this message "
            "window. Other calls and retained transcripts are outside this run."
        )
    if (
        scope["selected_messages"] < scope["imported_messages"]
        or scope["additional_calls"] < scope["matching_calls"]
    ) and data.get("outcome", "complete") == "complete":
        data["outcome"] = "partial"
