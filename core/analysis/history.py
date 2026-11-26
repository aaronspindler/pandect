"""Bounded same-case peers for comparisons anchored to newly added evidence."""

from collections import Counter
from datetime import timedelta

from django.conf import settings
from django.db.models import F, Q, Value
from django.db.models.functions import Abs, Extract, Least, Length

from core.analysis.sources import source_id
from core.evidence.queries import scoped_evidence
from core.evidence.types import CALL_KINDS, LOCATION_KINDS
from phone.models import CommunicationContext
from text.analysis.shared import STOPWORDS, WORD

MAX_HISTORY = 25


def _historical(case, batch, kind, choices):
    queryset = (
        scoped_evidence(case, kind)
        .filter(is_archived=False, created_at__lte=batch.created_at)
        .exclude(pk__in=[choice["id"] for choice in choices if choice["kind"] == kind])
    )
    if kind == "text-message" and batch.scope.get("type") == "messages":
        queryset = queryset.exclude(
            communication_context__import_run_id=batch.scope["id"]
        )
    elif kind == "vehicle-location" and batch.scope.get("type") == "vehicle":
        queryset = queryset.exclude(import_batch_id=batch.scope["id"])
    return queryset


def _time_near(field, moment, window):
    return Q(**{f"{field}__range": (moment - window, moment + window)})


def _nearest(queryset, time_field, times, count, *, include_text=False):
    distances = [
        Abs(Extract(F(time_field) - Value(moment), "epoch"))
        for moment in sorted(set(times))
    ]
    distance = Least(*distances) if len(distances) > 1 else distances[0]
    columns = ["pk", time_field]
    if include_text:
        queryset = queryset.annotate(body_length=Length("body"))
        columns.append("body_length")
    return queryset.order_by(distance, time_field, "pk").values_list(*columns)[:count]


def _fit(choices, rows, count, characters):
    selected = list(choices)
    for choice, length in rows:
        if len(selected) >= count:
            break
        if length <= characters:
            selected.append(choice)
            characters -= length
    return selected


def communication_selection(
    case,
    batch,
    choices,
    originals,
    *,
    minutes,
    include_calls=False,
    session_replies=False,
    count=None,
    characters=1_000_000,
    anchor_chars=0,
):
    count = min(settings.PANDECT_ANALYSIS_MAX_SOURCES, count or 500)
    timed = [
        item
        for item in originals
        if item.occurred_at is not None and item.sender_id and item.recipient_id
    ]
    peer_limit = min(MAX_HISTORY, count - len(choices))
    if not timed or peer_limit <= 0:
        return choices
    time_window = timedelta(minutes=minutes)
    moments = [item.occurred_at for item in timed]
    call_matches = Q(pk__in=[])
    message_matches = Q(pk__in=[])
    threads = {
        message: (document, thread)
        for message, document, thread in CommunicationContext.objects.filter(
            case=case,
            source_document__case=case,
            source_sha256=F("source_document__sha256"),
            message_id__in=[row.pk for row in timed],
        )
        .exclude(native_thread_id="")
        .values_list("message_id", "source_document_id", "native_thread_id")
    }
    for row in timed:
        same_pair = Q(sender_id=row.sender_id, recipient_id=row.recipient_id) | Q(
            sender_id=row.recipient_id, recipient_id=row.sender_id
        )
        near = _time_near("occurred_at", row.occurred_at, time_window)
        call_matches |= near & same_pair
        preferred = same_pair
        if thread := threads.get(row.pk):
            preferred |= Q(
                communication_context__case=case,
                communication_context__source_document_id=thread[0],
                communication_context__native_thread_id=thread[1],
                communication_context__source_sha256=F(
                    "communication_context__source_document__sha256"
                ),
            )
        message_matches |= near & preferred
    peers = []
    for kind in ("text-message", *CALL_KINDS) if include_calls else ("text-message",):
        use_text = kind == "text-message"
        query = _historical(case, batch, kind, choices).filter(
            message_matches if use_text else call_matches
        )
        for pk, moment, *length in _nearest(
            query, "occurred_at", moments, peer_limit, include_text=use_text
        ):
            nearest = min(abs(moment - time) for time in moments)
            peers.append((nearest, moment, str(pk), kind, length[0] if use_text else 0))
    if session_replies and len(peers) < peer_limit and characters > anchor_chars:
        from phone.grouping_sources import delayed_reply_peers

        existing = {pk for _, _, pk, _, _ in peers}
        peers.extend(
            candidate
            for candidate in delayed_reply_peers(
                case, batch, timed, minutes, characters - anchor_chars
            )
            if candidate[2] not in existing
        )
    return _fit(
        choices,
        (({"kind": kind, "id": pk}, size) for _, _, pk, kind, size in sorted(peers)),
        len(choices) + peer_limit,
        characters - anchor_chars,
    )


def location_selection(case, batch, kind, choices, originals, operation):
    limit = min(settings.PANDECT_ANALYSIS_MAX_SOURCES, len(choices) + MAX_HISTORY)
    timed = [item for item in originals if item.observed_at is not None]
    peer_limit = limit - len(choices)
    if not timed or peer_limit <= 0:
        return choices
    members = ("vehicle_id", "person_id")
    window = (
        timedelta(days=30)
        if operation == "movement_routines"
        else timedelta(minutes=10 if operation == "colocation" else 60)
    )
    times = [item.observed_at for item in timed]
    rows = []
    for peer_kind in LOCATION_KINDS:
        same_subject = operation in {
            "movement_routines",
            "candidate_trips",
            "candidate_visits",
            "travel_discrepancies",
        }
        if same_subject and peer_kind != kind:
            continue
        match = Q(pk__in=[])
        for item in timed:
            near = _time_near("observed_at", item.observed_at, window)
            subject = {
                field: getattr(item, field) for field in members if hasattr(item, field)
            }
            if same_subject:
                if not subject:
                    continue
                near &= Q(**subject)
            elif operation == "colocation" and kind == peer_kind:
                if not subject:
                    continue
                near &= ~Q(**subject)
            match |= near
        query = _historical(case, batch, peer_kind, choices).filter(
            match, latitude__isnull=False, longitude__isnull=False
        )
        for pk, moment in _nearest(query, "observed_at", times, peer_limit):
            rows.append(
                (min(abs(moment - time) for time in times), moment, str(pk), peer_kind)
            )
    return _fit(
        choices,
        (({"kind": kind, "id": pk}, 0) for _, _, pk, kind in sorted(rows)),
        limit,
        0,
    )


def wording_selection(case, batch, choices, anchor_texts):
    limit = min(50, settings.PANDECT_ANALYSIS_MAX_SOURCES)
    characters = 100_000 - sum(map(len, anchor_texts))
    if len(choices) >= limit or characters <= 0:
        return choices
    words = Counter(
        word
        for body in anchor_texts
        for word in (
            match.group().casefold() for match in WORD.finditer(body[:100_000])
        )
        if len(word) >= 4 and word not in STOPWORDS
    )
    terms = sorted(words, key=lambda word: (-words[word], word))[:8]
    if not terms:
        return choices
    if not case.external_ai_enabled or batch.scope.get("search_revision") != str(
        case.external_ai_revision
    ):
        return choices
    from core.case_search.hosted import HostedSearchError, search

    try:
        hits = search(
            case,
            batch.requested_by,
            " ".join(terms),
            through=batch.created_at,
            limit=MAX_HISTORY,
        )
    except HostedSearchError:
        return choices
    candidates = [
        ref
        for locator, _ in hits
        for ref in locator.get("dependencies", ())
        if ref["kind"] in ("text", "text-message")
    ]
    available = {}
    for kind in ("text", "text-message"):
        ids = {ref["id"] for ref in candidates if ref["kind"] == kind}
        for pk, length in (
            _historical(case, batch, kind, choices)
            .filter(pk__in=ids)
            .annotate(length=Length("body"))
            .values_list("pk", "length")
        ):
            available[kind, str(pk)] = length
    unique = dict.fromkeys((ref["kind"], ref["id"]) for ref in candidates)
    return _fit(
        choices,
        (
            ({"kind": kind, "id": pk}, available[kind, pk])
            for kind, pk in unique
            if (kind, pk) in available
        ),
        limit,
        characters,
    )


def comparison_parameters(anchors, selected, parameters):
    return parameters | {
        "_comparison": {
            "anchors": [source_id(choice) for choice in anchors],
            "earlier_sources": len(selected) - len(anchors),
        }
    }


def describe_anchored_comparison(data, run):
    scope = run.parameters["_comparison"]
    anchors = set(scope["anchors"])
    items = data.get("items", [])
    selected = [
        item for item in items if anchors.intersection(item.get("source_ids", []))
    ]
    if len(selected) < len(items):
        data["items"] = selected
        data["summary"] = (
            f"Retained {len(selected)} findings that cite newly selected evidence."
        )
        if not selected and data.get("outcome", "complete") == "complete":
            data["outcome"] = "empty"
    data.setdefault("metrics", {})["Comparison coverage"] = {
        "Newly selected sources": len(anchors),
        "Earlier sources": scope["earlier_sources"],
        "Findings citing newly selected sources": len(selected),
    }
    data.setdefault("warnings", []).append(
        "This comparison covers the retained selection. Findings based only on "
        "earlier sources are omitted."
    )
