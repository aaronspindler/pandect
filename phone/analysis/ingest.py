"""Automatic, bounded analysis of newly published CSV messages."""

from collections import defaultdict
from datetime import timedelta
from uuid import uuid5

from django.conf import settings
from django.db.models import F, Q

from core.analysis.bulk import start_case_analysis
from core.analysis.import_checks import imported_sources
from core.analysis.services import start_run
from core.analysis.sources import source_id
from core.evidence.queries import scoped_evidence


def start_import_analysis(run, data):
    """Commit the preparation task with publication; retries keep one batch."""
    if data["metrics"].get("created_rows"):
        start_case_analysis(
            run.requested_by,
            run.case_id,
            submission_id=uuid5(run.pk, "imessage-analysis-v1"),
            scope={"type": "messages", "id": str(run.pk), "automatic": True},
        )


def next_messages(batch, case):
    """Resume in reported-time order, with unknown times last and stable ties."""
    _, messages = imported_sources(case, batch.scope)
    messages = messages.filter(created_at__lte=batch.created_at)
    if batch.source_cursor:
        previous = messages.get(pk=batch.source_cursor)
        if previous.occurred_at is None:
            messages = messages.filter(occurred_at__isnull=True, pk__gt=previous.pk)
        else:
            messages = messages.filter(
                Q(occurred_at__gt=previous.occurred_at)
                | Q(occurred_at=previous.occurred_at, pk__gt=previous.pk)
                | Q(occurred_at__isnull=True)
            )
    messages = messages.order_by(F("occurred_at").asc(nulls_last=True), "pk")
    # Leave room for same-participant call metadata in cross-channel comparisons.
    limit = min(250, settings.PANDECT_ANALYSIS_MAX_SOURCES)
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
    _, imported = imported_sources(case, batch.scope)
    total = imported.count()
    limit = settings.PANDECT_ANALYSIS_MAX_SOURCES

    def queue(operation, selected, parameters=None, *, additions=(), matching_calls=0):
        choices = [{"kind": "text-message", "id": str(item.pk)} for item in selected]
        choices.extend(additions)
        scope = {
            "import_run_id": batch.scope["id"],
            "imported_messages": total,
            "selected_messages": len(selected),
            "additional_calls": len(additions),
            "matching_calls": matching_calls,
        }
        _, created = start_run(
            batch.requested_by,
            case.pk,
            operation,
            choices,
            parameters=(parameters or {}) | {"_import_analysis": scope},
            submission_id=uuid5(
                batch.pk,
                f"{operation}:{selected[0].pk}:{parameters or {}}",
            ),
            batch=batch,
        )
        batch.queued_runs += created

    queue("entities", messages)
    queue("text_topics", messages, {"include_summary": False})
    queue("sessions", messages, {"gap_minutes": 60})
    queue("comm_patterns", messages, {"gap_minutes": 60})

    calls, matching_calls = [], 0
    timed = [message for message in messages if message.occurred_at is not None]
    if timed:
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
    for group in _groups(messages, count=min(20, limit), characters=16_000):
        queue("text_topics", group, {"include_summary": True, "backend": "local"})
        queue("content_events", group, {"backend": "local"})
        queue("session_summaries", group, {"gap_minutes": 60, "backend": "local"})
        queue("conversation_link", group, {"semantic_links": True, "backend": "local"})


def describe_import_selection(data, run):
    """Disclose bounded coverage rather than implying whole-conversation analysis."""
    scope = run.parameters.get("_import_analysis")
    if scope is None:
        return
    data["metrics"]["import_selection"] = scope
    data["warnings"].append(
        f"Automatic analysis of {scope['selected_messages']} of "
        f"{scope['imported_messages']} newly imported messages. "
        "Each run covers only its retained selection. Comparisons do not span "
        "separate selections; a selection boundary can cut an exchange."
    )
    if run.operation in {"style_profiles", "style_change"}:
        data["warnings"].append(
            "Writing samples group the reported sender. Comparisons split that "
            "sender's timed samples at a distinct timestamp near the middle."
        )
    if run.operation == "conversation_link" and not run.parameters.get(
        "semantic_links"
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
