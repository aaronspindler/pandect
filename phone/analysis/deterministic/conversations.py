"Reviewable conversation grouping and observed interaction measurements."

import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime

from django.core.exceptions import ValidationError
from django.db.models import Exists, F, OuterRef
from django.utils.dateparse import parse_datetime

from core.analysis.sources import output
from core.evidence.types import COMMUNICATION_KINDS
from phone.message_csv import WHATSAPP_PARSER_VERSION
from phone.models import CommunicationContext
from phone.session_matching import (
    SESSION_RULES_VERSION,
    message_context,
    participant_stream,
    session_decision,
)
from text.analysis.shared import STOPWORDS, WORD, bounded_sources

VERSION = "conversation-rules-v1"
DEFAULT_SESSION_GAP_MINUTES = 120


def _number(parameters, key, default, minimum, maximum):
    value = parameters.get(key, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not minimum <= value <= maximum
    ):
        raise ValidationError(f"{key} must be between {minimum} and {maximum}.")
    return float(value)


def _time(source):
    value = getattr(source.item, "occurred_at", None)
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValidationError("Communication event times must include their timezone.")
    return value


def _ordered(sources, *, messages_only=False):
    selected = [
        s
        for s in bounded_sources(sources)
        if s.kind in ({"text-message"} if messages_only else COMMUNICATION_KINDS)
    ]
    selected.sort(
        key=lambda s: (
            _time(s) is None,
            _time(s) or datetime.max.replace(tzinfo=UTC),
            str(s.item.pk),
            str(s.id),
        )
    )
    return selected


def _source_row(source):
    context = message_context(source.item)
    row = {
        "source_id": str(source.id),
        "channel": source.kind,
        "occurred_at": _time(source).isoformat() if _time(source) else None,
        "sender_id": str(source.item.sender_id),
        "recipient_id": str(source.item.recipient_id),
        "text": source.text,
        "url": source.url,
        "native_message_id": getattr(context, "native_message_id", ""),
        "native_thread_id": getattr(context, "native_thread_id", ""),
        "reply_to_id": getattr(context, "reply_to_id", ""),
    }
    if getattr(context, "parser_version", "") == WHATSAPP_PARSER_VERSION:
        row["source_document_id"] = str(context.source_document_id)
    return row


def _native_key(source, field):
    context = message_context(source.item)
    value = getattr(context, field, "")
    if value.strip():
        return (source.item.case_id, context.source_document_id, value)
    return None


def _selected_reply_targets(messages):
    """Only a unique retained native ID in the reply's export can link originals."""
    referenced = {_native_key(source, "reply_to_id") for source in messages}
    referenced.discard(None)
    if not referenced:
        return {}
    targets = {}
    context_ids = []
    for source in messages:
        key = _native_key(source, "native_message_id")
        if key not in referenced:
            continue
        targets[key] = None if key in targets else source
        context = message_context(source.item)
        if isinstance(context, CommunicationContext):
            context_ids.append(context.pk)
    if context_ids:
        # A duplicate outside this analysis selection still makes the pointer ambiguous.
        matches = CommunicationContext.objects.filter(
            case_id=OuterRef("case_id"),
            source_document_id=OuterRef("source_document_id"),
            native_message_id=OuterRef("native_message_id"),
        ).exclude(pk=OuterRef("pk"))
        usable = (
            CommunicationContext.objects.filter(
                pk__in=context_ids,
                case_id=F("message__case_id"),
                source_document__case_id=F("case_id"),
                source_sha256=F("source_document__sha256"),
            )
            .exclude(Exists(matches))
            .values_list("message_id", flat=True)
        )
        usable_ids = set(usable)
        targets = {
            key: target
            for key, target in targets.items()
            if target is not None and target.item.pk in usable_ids
        }
    return targets


def _session_groups(sources, parameters):
    messages = _ordered(sources, messages_only=True)
    gap_minutes = _number(
        parameters, "gap_minutes", DEFAULT_SESSION_GAP_MINUTES, 1, 10080
    )
    break_before = parameters.get("break_before", [])
    join_before = parameters.get("join_before", [])
    if not isinstance(break_before, list) or not isinstance(join_before, list):
        raise ValidationError("Session adjustments must list selected source IDs.")
    breaks, joins = set(map(str, break_before)), set(map(str, join_before))
    ids = {str(s.id) for s in messages}
    if (breaks | joins) - ids or breaks & joins:
        raise ValidationError(
            "Session adjustments refer to unavailable or conflicting source IDs."
        )
    groups = []
    active = {}
    targets = _selected_reply_targets(messages)
    membership = {}
    for source in messages:
        key = participant_stream(source.item)
        group = active.get(key)
        previous = group["sources"][-1] if group else None
        parent = targets.get(_native_key(source, "reply_to_id"))
        if parent and membership.get(parent.id) is not group:
            parent = None
        decision = session_decision(
            previous.item if previous else None,
            source.item,
            gap_minutes=gap_minutes,
            native_parent=parent.item if parent else None,
            break_before=str(source.id) in breaks,
            join_before=str(source.id) in joins,
            greeting_boundaries=parameters.get("greeting_boundaries", True),
        )
        if decision.boundary:
            group = {
                "sources": [],
                "reason": decision.boundary,
                "adjusted": False,
                "continuations": [],
            }
            groups.append(group)
            active[key] = group
        if decision.continuation:
            target = parent or previous
            group["continuations"].append(
                {
                    "reason": decision.continuation,
                    "elapsed_seconds": decision.elapsed_seconds,
                    "source_ids": [str(target.id), str(source.id)],
                }
            )
        group["sources"].append(source)
        membership[source.id] = group
        if str(source.id) in breaks or str(source.id) in joins:
            group["adjusted"] = True
        if _time(source) is None:
            active.pop(key, None)
    return messages, groups, gap_minutes


def conversation_sessions(sources, parameters):
    messages, groups, gap = _session_groups(sources, parameters)
    items = []
    for index, group in enumerate(groups, 1):
        rows = group["sources"]
        first, last = _time(rows[0]), _time(rows[-1])
        contexts = [message_context(s.item) for s in rows]
        native_threads = sorted(
            {
                f"{c.source_document_id}:{c.native_thread_id}"
                for c in contexts
                if c and c.native_thread_id
            }
        )
        items.append(
            {
                "label": f"Inferred session {index}",
                "text": group["reason"],
                "source_ids": [str(s.id) for s in rows],
                "anchors": [s.anchor(start=0, end=len(s.text)) for s in rows],
                "values": {
                    "messages": [_source_row(s) for s in rows],
                    "message_count": len(rows),
                    "first_recorded_at": first.isoformat() if first else None,
                    "last_recorded_at": last.isoformat() if last else None,
                    "observed_span_seconds": (last - first).total_seconds()
                    if first and last
                    else None,
                    "native_thread_references": native_threads,
                    "investigator_adjusted": group["adjusted"],
                    "delayed_continuations": group["continuations"],
                    "grouping": (
                        "An inferred session based on reported participants, times, "
                        "reply references and wording."
                    ),
                },
            }
        )
    return output(
        (
            f"Proposed {len(groups)} inferred sessions "
            f"from {len(messages)} selected messages."
        ),
        items=items,
        metrics={
            "selected_messages": len(messages),
            "sessions": len(groups),
            "missing_event_times": sum(_time(s) is None for s in messages),
            "gap_minutes": gap,
            "method": SESSION_RULES_VERSION,
        },
        warnings=[
            (
                "Grouping uses selected records and disclosed "
                "heuristics. Missing exchanges, simultaneous topics, "
                "and uncertain attribution can split or combine "
                "discussions. Inferred session numbers are not native "
                "thread IDs."
            ),
            (
                "Adjustments are retained in run parameters; rerun with"
                " break_before/join_before to preserve a further "
                "grouping version without modifying originals."
            ),
        ],
        outcome="complete" if messages else "insufficient",
    )


def _pattern_metrics(rows, session_groups):
    timed = [s for s in rows if _time(s) is not None]
    selected_ids = {str(s.id) for s in rows}
    intervals = []
    apparent_initiations = Counter()
    durations = []
    selected_sessions = []
    for number, group in enumerate(session_groups, 1):
        selected = [s for s in group["sources"] if str(s.id) in selected_ids]
        if not selected:
            continue
        selected_sessions.append(number)
        original_first = group["sources"][0]
        if str(original_first.id) in selected_ids and _time(original_first):
            apparent_initiations[str(original_first.item.sender_id)] += 1
        first, last = _time(selected[0]), _time(selected[-1])
        durations.append((last - first).total_seconds() if first and last else None)
        for previous, current in zip(selected, selected[1:], strict=False):
            if (
                _time(previous) is not None
                and _time(current) is not None
                and current.item.sender_id != previous.item.sender_id
                and current.item.sender_id != current.item.recipient_id
                and previous.item.sender_id != previous.item.recipient_id
            ):
                intervals.append((_time(current) - _time(previous)).total_seconds())
    return {
        "records": len(rows),
        "timed_records": len(timed),
        "missing_event_times": len(rows) - len(timed),
        "counts_by_sender_id": dict(Counter(str(s.item.sender_id) for s in rows)),
        "counts_by_channel": dict(Counter(s.kind for s in rows)),
        "recorded_utc_hours": dict(
            sorted(Counter(str(_time(s).hour) for s in timed).items())
        ),
        "first_recorded_at": min(_time(s) for s in timed).isoformat()
        if timed
        else None,
        "last_recorded_at": max(_time(s) for s in timed).isoformat() if timed else None,
        "observed_turn_changes": len(intervals),
        "response_interval_count": len(intervals),
        "response_intervals_seconds": intervals,
        "median_response_interval_seconds": statistics.median(intervals)
        if intervals
        else None,
        "apparent_session_initiations_by_sender": dict(apparent_initiations),
        "session_spans_seconds": durations,
        "inferred_session_numbers": selected_sessions,
    }


def interaction_patterns(sources, parameters):
    rows, groups, gap = _session_groups(sources, parameters)
    metrics = _pattern_metrics(rows, groups)
    metrics |= {
        "method": SESSION_RULES_VERSION,
        "gap_minutes": gap,
        "inferred_sessions": len(groups),
    }
    if not rows:
        return output(
            "No selected messages are available for interaction analysis.",
            metrics=metrics,
            outcome="insufficient",
        )
    items = [
        {
            "label": "Selected message",
            "text": s.text,
            "source_ids": [str(s.id)],
            "anchors": [s.anchor(start=0, end=len(s.text))],
            "values": _source_row(s),
        }
        for s in rows
    ]
    for number, group in enumerate(groups, 1):
        selected = group["sources"]
        items.append(
            {
                "label": f"Observed session {number}",
                "text": group["reason"],
                "source_ids": [str(s.id) for s in selected],
                "anchors": [s.anchor(start=0, end=len(s.text)) for s in selected],
                "values": _pattern_metrics(selected, groups)
                | {
                    "investigator_adjusted": group["adjusted"],
                    "delayed_continuations": group["continuations"],
                    "source_order": [str(s.id) for s in selected],
                    "grouping": (
                        "Same inferred grouping and adjustments as the session "
                        "reader; not native thread identity"
                    ),
                },
            }
        )
    split = parameters.get("compare_split_at")
    if split:
        moment = parse_datetime(str(split))
        if moment is None or moment.tzinfo is None:
            raise ValidationError(
                "Comparison split time must be an ISO timestamp with timezone."
            )
        for label, group in (
            (
                "Before selected split",
                [s for s in rows if _time(s) and _time(s) < moment],
            ),
            (
                "At/after selected split",
                [s for s in rows if _time(s) and _time(s) >= moment],
            ),
        ):
            items.append(
                {
                    "label": label,
                    "text": (
                        "Compare selected-source coverage before interpreting "
                        "activity changes. A split does not create a new "
                        "session initiation."
                    ),
                    "source_ids": [str(s.id) for s in group or rows],
                    "anchors": [],
                    "values": _pattern_metrics(group, groups)
                    | {
                        "contributing_source_ids": [str(s.id) for s in group],
                        "comparison_split_at": moment.isoformat(),
                    },
                }
            )
    return output(
        "Observed interaction counts and intervals for selected messages.",
        items=items,
        metrics=metrics,
        warnings=[
            (
                "Response intervals use consecutive selected messages "
                "from different senders inside the exact inferred "
                "sessions and investigator adjustments shown here. They"
                " are not reading times or proof of a response to a "
                "particular message."
            ),
            (
                "Apparent initiation counts use the original first "
                "timed message of each inferred session. "
                "Comparison-period fragments do not invent new "
                "initiations. Unknown-time sessions have unknown spans."
            ),
            (
                "Missing export periods are unknown coverage, not "
                "inactivity, avoidance, motives, emotions, or "
                "relationship quality. Times are UTC."
            ),
        ],
        outcome="complete" if rows else "insufficient",
    )


def cross_channel_links(sources, parameters):
    rows = [
        source
        for source in _ordered(sources)
        if source.item.sender_id and source.item.recipient_id
    ]
    window = _number(
        parameters,
        "window_minutes",
        parameters.get("time_window_minutes", 180),
        0,
        10080,
    )
    by_pair = defaultdict(list)
    for source in rows:
        if _time(source):
            by_pair[
                participant_stream(source.item, include_reported_pair=False)
            ].append(source)
    items = []
    omitted_temporal = 0
    for pair in by_pair.values():
        for left, right in zip(pair, pair[1:], strict=False):
            gap = (_time(right) - _time(left)).total_seconds()
            if gap > window * 60:
                continue
            if len(items) >= 250:
                omitted_temporal += 1
                continue
            a = {m.group().casefold() for m in WORD.finditer(left.text)} - STOPWORDS
            b = {m.group().casefold() for m in WORD.finditer(right.text)} - STOPWORDS
            common = sorted(w for w in a & b if len(w) >= 4)
            items.append(
                {
                    "label": "Candidate exchange sequence",
                    "text": "Recorded temporal adjacency"
                    + (" and shared wording" if common else ""),
                    "source_ids": [str(left.id), str(right.id)],
                    "anchors": [
                        s.anchor(start=0, end=len(s.text))
                        for s in (left, right)
                        if s.text
                    ],
                    "values": {
                        "before": _source_row(left),
                        "after": _source_row(right),
                        "elapsed_seconds": gap,
                        "shared_words": common[:20],
                        "basis": (
                            "temporal adjacency; lexical overlap is a separate weak"
                            " content cue"
                        ),
                        "interpretation": (
                            "Unresolved: proximity in time or subject wording does "
                            "not prove the same event, a reference, discussion "
                            "content, or causation."
                        ),
                    },
                }
            )
    warnings = [
        (
            "Metadata-only calls contribute recorded ordering and "
            "participants only. Missing exchanges and ambiguous "
            "references remain unresolved; these links do not "
            "verify relationship or causation."
        )
    ]
    outcome = "partial" if omitted_temporal else "complete" if items else "empty"
    if omitted_temporal:
        warnings.append(
            f"{omitted_temporal} temporal candidates exceed the 250-item "
            "display limit; select fewer communications to inspect them."
        )
    return output(
        "Candidate links across selected messages and calls.",
        items=items,
        metrics={
            "selected_communications": len(rows),
            "missing_event_times": sum(_time(s) is None for s in rows),
            "window_minutes": window,
            "omitted_temporal_candidates": omitted_temporal,
            "method": VERSION,
        },
        warnings=warnings,
        outcome=outcome,
    )
