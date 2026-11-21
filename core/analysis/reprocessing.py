"""Prepare bounded ingest and reprocessing runs with retained execution routes."""

import json
from datetime import timedelta
from functools import partial
from uuid import uuid5

from allauth.account.models import EmailAddress
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F, Q
from django.http import Http404

from cases.permissions import get_case
from core.analysis.errors import AnalysisError, error_message
from core.analysis.execution import (
    GEOGRAPHIC,
    available_executions,
    requires_inference,
    snapshot_execution,
)
from core.analysis.history import (
    communication_selection,
    comparison_parameters,
    location_selection,
    wording_selection,
)
from core.analysis.services import snapshot_sources, start_run
from core.analysis.sources import source_id
from core.evidence.queries import get_evidence, scoped_evidence
from core.evidence.types import CALL_KINDS, COMMUNICATION_KINDS, KINDS, LOCATION_KINDS
from core.models import CaseAnalysisBatch
from phone.models import CallEvidenceAttachment

CONTEXT_REQUIRED_OPERATIONS = (
    "question_synthesis",
    "sighting_group",
    "style_change",
    "proximity",
    "location_conflicts",
    "journey_claims",
    "imessage_import",
)
REPROCESS_OPERATIONS = (
    "native_text",
    "image_metadata",
    "audio_metadata",
    "ocr",
    "plate_ocr",
    "transcription",
    "entities",
    "style_profiles",
    "copied_text",
    "text_topics",
    "content_events",
    "session_summaries",
    "sessions",
    "comm_patterns",
    "conversation_link",
    "normalize_addresses",
    "location_quality",
    "spatial_groups",
    "geographic_context",
    "candidate_visits",
    "candidate_trips",
    "movement_routines",
    "colocation",
    "travel_discrepancies",
    "geocode_locations",
)
MODEL_PARAMETERS = {
    "transcription": {},
    "text_topics": {"include_summary": True},
    "content_events": {},
    "session_summaries": {"gap_minutes": 60},
    "conversation_link": {"semantic_links": True, "time_window_minutes": 10},
}
MODE_LABELS = {
    "local": "Local checks and private services",
    "external": "External services",
    "both": "Local, private and external services",
}
ANALYSIS_VERSION = 2


def reprocessing_executions():
    """Return reviewable routes, never credentials or a connectivity probe."""
    routes = {
        f"{operation}:{backend}": snapshot_execution(
            operation, parameters | {"backend": backend}
        )
        for operation, parameters in MODEL_PARAMETERS.items()
        for backend in ("local", "gateway")
    }
    for operation in ("geocode_locations", "geographic_context"):
        routes[operation] = snapshot_execution(operation, {})
    return routes


def analysis_scope(case, kinds):
    """Freeze configured routes eligible under the current case policy."""
    kinds = set(kinds)
    audio_routes = (
        available_executions(case, "transcription", {})
        if kinds.intersection((*CALL_KINDS, "audio"))
        else {}
    )
    operations = []
    if kinds - {"audio", *LOCATION_KINDS} or audio_routes:
        operations.extend(("text_topics", "content_events"))
    if "text-message" in kinds:
        operations.append("session_summaries")
    if kinds.intersection(COMMUNICATION_KINDS):
        operations.append("conversation_link")
    if kinds.intersection(LOCATION_KINDS):
        operations.extend(GEOGRAPHIC)
    executions = {
        f"transcription:{backend}": route for backend, route in audio_routes.items()
    }
    for operation in operations:
        for backend, route in available_executions(
            case, operation, MODEL_PARAMETERS.get(operation, {})
        ).items():
            key = operation if operation in GEOGRAPHIC else f"{operation}:{backend}"
            executions[key] = route
    return {
        "analysis_version": ANALYSIS_VERSION,
        "executions": executions,
        "external_grant_revision": case.external_analysis_approval.get("revision", ""),
    }


def validate_reprocessing_executions(batch):
    current = reprocessing_executions()
    if any(
        current.get(key) != value for key, value in batch.scope["executions"].items()
    ):
        raise AnalysisError("execution_changed")


@transaction.atomic
def start_reprocessing(actor, case_id, *, submission_id, kinds):
    case = get_case(actor, case_id, edit=True, lock=True)
    if not isinstance(kinds, (list, tuple)) or not kinds or set(kinds) - KINDS.keys():
        raise ValidationError("Choose at least one supported evidence type.")
    kinds = sorted(set(kinds))
    existing = CaseAnalysisBatch.objects.filter(pk=submission_id).first()
    if existing:
        if (
            existing.case_id != case.pk
            or existing.requested_by_id != actor.pk
            or existing.scope.get("type") != "reprocess"
            or existing.scope.get("kinds") != kinds
        ):
            raise ValidationError(
                "This submission was already used for another analysis."
            )
        return existing, False
    scope = {"type": "reprocess", "kinds": kinds} | analysis_scope(case, kinds)
    from core.analysis.bulk import start_case_analysis

    return start_case_analysis(actor, case.pk, submission_id=submission_id, scope=scope)


def _originals(batch, case, kind):
    return scoped_evidence(case, kind).filter(
        is_archived=False, created_at__lte=batch.created_at
    )


def _source_time_field(kind, additions):
    if additions:
        return "created_at"
    if kind in COMMUNICATION_KINDS:
        return "occurred_at"
    if kind in LOCATION_KINDS:
        return "observed_at"
    return "created_at"


def _next_page(batch, case):
    from core.analysis.bulk import retained_cursor_time

    phases = [
        phase
        for kind in batch.scope["kinds"]
        for phase in ((kind, f"{kind}:additions") if kind in CALL_KINDS else (kind,))
    ]
    for phase in phases:
        if batch.source_kind and phases.index(phase) < phases.index(batch.source_kind):
            continue
        kind, _, additions = phase.partition(":")
        originals = _originals(batch, case, kind)
        if additions:
            parent = "phone_call" if kind == "phone-call" else "facetime"
            queryset = CallEvidenceAttachment.objects.filter(
                case=case,
                created_at__lte=batch.created_at,
                **{f"{parent}__in": originals.values("pk")},
            ).select_related(parent)
        else:
            queryset = originals
        time_field = _source_time_field(kind, additions)
        if phase == batch.source_kind and batch.source_cursor:
            if "_cursor" in batch.scope:
                moment = retained_cursor_time(batch)
            else:
                model = CallEvidenceAttachment if additions else KINDS[kind]
                previous = model.objects.filter(
                    case=case, pk=batch.source_cursor
                ).first()
                if previous is None:
                    raise AnalysisError("source_unavailable")
                moment = getattr(previous, time_field)
            if moment is None:
                queryset = queryset.filter(
                    **{f"{time_field}__isnull": True, "pk__gt": batch.source_cursor}
                )
            else:
                queryset = queryset.filter(
                    Q(**{f"{time_field}__gt": moment})
                    | Q(**{time_field: moment, "pk__gt": batch.source_cursor})
                    | Q(**{f"{time_field}__isnull": True})
                )
        limit = min(25, settings.PANDECT_ANALYSIS_MAX_SOURCES)
        rows = list(queryset.order_by(F(time_field).asc(nulls_last=True), "pk")[:limit])
        if rows:
            batch.source_kind = phase
            return kind, bool(additions), rows
    return "", False, []


def _error(batch, kind, evidence_id, code, *, operation=""):
    # Reprocessing counts failed preparation attempts, independently of its
    # bounded error sample. Legacy local batches keep counting unavailable sources.
    batch.unavailable_sources += 1
    if len(batch.preparation_errors) < 100:
        batch.preparation_errors.append(
            {
                "kind": kind,
                "evidence_id": str(evidence_id),
                "operation": operation,
                "message": error_message(code),
            }
        )


def _queue(batch, operation, choices, parameters=None, *, marker="", previous_run=None):
    if not choices:
        return
    parameters = parameters or {}
    purpose = requires_inference(operation, parameters)
    key = f"{operation}:{parameters.get('backend', 'local')}" if purpose else operation
    route = batch.scope["executions"].get(key)
    if (
        purpose or operation in {"geocode_locations", "geographic_context"}
    ) and not route:
        return
    versioned = batch.scope.get("analysis_version") == ANALYSIS_VERSION
    parameters = parameters | {
        "_reprocessing": {
            "batch_id": str(batch.pk),
            "selected_sources": len(choices),
            "bounded_comparison": True,
        }
    }
    identity = (
        json.dumps([operation, choices, parameters], sort_keys=True)
        if versioned
        else f"{batch.source_kind}:{operation}:{choices[0]}:{marker}:"
        f"{parameters.get('backend', '')}:{parameters.get('reverse', '')}"
    )
    submission_id = uuid5(batch.pk, identity)
    try:
        _, created = start_run(
            batch.requested_by,
            batch.case_id,
            operation,
            choices,
            parameters=parameters,
            submission_id=submission_id,
            batch=batch,
            previous_run=previous_run,
            approved_execution=route,
            external_grant_revision=batch.scope.get("external_grant_revision"),
        )
    except (AnalysisError, ValidationError, Http404) as error:
        _error(
            batch,
            choices[0]["kind"],
            choices[0]["id"],
            error.code if isinstance(error, AnalysisError) else "source_unavailable",
            operation=operation,
        )
    else:
        batch.queued_runs += created


def _model_checks(
    batch, operation, choices, parameters=None, *, marker="", previous_run=None
):
    parameters = parameters or {}
    if batch.scope.get("analysis_version") == ANALYSIS_VERSION and operation in {
        "text_topics",
        "conversation_link",
    }:
        parameters = parameters | {"_suggestions_only": True}
    for backend in ("local", "gateway"):
        _queue(
            batch,
            operation,
            choices,
            parameters | {"backend": backend},
            marker=marker,
            previous_run=previous_run,
        )


def _has_model_route(batch, operation):
    return any(
        f"{operation}:{backend}" in batch.scope["executions"]
        for backend in ("local", "gateway")
    )


def _groups(entries, *, count, characters):
    group, size = [], 0
    for choice, length in entries:
        if group and (len(group) >= count or size + length > characters):
            yield group
            group, size = [], 0
        group.append(choice)
        size += length
    if group:
        yield group


def _text_checks(
    batch,
    entries,
    *,
    writing=False,
    messages=False,
    communications=False,
    marker="",
    previous_run=None,
    history=None,
):
    queue = partial(_queue, batch, marker=marker, previous_run=previous_run)
    models = partial(_model_checks, batch, marker=marker, previous_run=previous_run)
    limit = settings.PANDECT_ANALYSIS_MAX_SOURCES
    local_checks = batch.scope.get("mode") != "external"
    for choices in _groups(entries, count=limit, characters=1_000_000):
        if local_checks:
            queue("entities", choices)
            queue(
                "text_topics",
                choices,
                {"include_summary": False},
                marker=f"keywords:{marker}",
            )
            if writing:
                queue("style_profiles", choices)
            if messages:
                group, parameters = _history_communication(
                    batch, choices, {"gap_minutes": 60}, history, minutes=60
                )
                queue("sessions", group, parameters)
                queue("comm_patterns", group, parameters)
    if local_checks:
        for choices in _groups(entries, count=min(50, limit), characters=100_000):
            selection, parameters = choices, {}
            if history:
                case, rows = history
                selection = wording_selection(
                    case,
                    batch,
                    choices,
                    [rows[source_id(choice)][1] for choice in choices],
                )
                parameters = comparison_parameters(choices, selection, {})
            if (
                len(selection) > 1
                or batch.scope.get("analysis_version") != ANALYSIS_VERSION
            ):
                queue("copied_text", selection, parameters)
    for choices in _groups(entries, count=min(20, limit), characters=16_000):
        models("text_topics", choices, {"include_summary": True})
        models("content_events", choices)
        if messages and _has_model_route(batch, "session_summaries"):
            group, parameters = _history_communication(
                batch, choices, {"gap_minutes": 60}, history, minutes=60, model=True
            )
            models("session_summaries", group, parameters)
        if communications:
            if local_checks:
                group, parameters = _history_communication(
                    batch, choices, {"time_window_minutes": 10}, history, minutes=10
                )
                queue("conversation_link", group, parameters)
            if _has_model_route(batch, "conversation_link"):
                group, parameters = _history_communication(
                    batch,
                    choices,
                    {"semantic_links": True, "time_window_minutes": 10},
                    history,
                    minutes=10,
                    model=True,
                )
                models("conversation_link", group, parameters)


def _history_communication(
    batch, choices, parameters, history, *, minutes, model=False
):
    if not history:
        return choices, parameters
    case, rows = history
    selected = [rows[source_id(choice)] for choice in choices]
    combined = communication_selection(
        case,
        batch,
        choices,
        [row for row, _ in selected],
        minutes=minutes,
        include_calls=minutes == 10,
        count=20 if model else None,
        characters=16_000 if model else 1_000_000,
        anchor_chars=sum(len(body) for _, body in selected),
    )
    return combined, comparison_parameters(choices, combined, parameters)


def _communication_checks(batch, case, kind, rows, choices):
    if not choices:
        return
    history = None
    if batch.scope["type"] == "added":
        history = (
            case,
            {
                source_id(choice): (row, getattr(row, "body", ""))
                for row, choice in zip(rows, choices, strict=True)
            },
        )
    combined = list(choices)
    timed = [row for row in rows if row.occurred_at is not None]
    if timed:
        for other_kind in batch.scope.get("kinds", []):
            if other_kind not in COMMUNICATION_KINDS or other_kind == kind:
                continue
            # Message pages already compare their selected calls. Avoid pulling
            # an unbounded amount of message text into a call-metadata page.
            if other_kind == "text-message":
                continue
            available = _originals(batch, case, other_kind).filter(
                occurred_at__gte=timed[0].occurred_at - timedelta(minutes=10),
                occurred_at__lte=timed[-1].occurred_at + timedelta(minutes=10),
            )
            slots = settings.PANDECT_ANALYSIS_MAX_SOURCES - len(combined)
            combined.extend(
                {"kind": other_kind, "id": str(pk)}
                for pk in available.order_by("occurred_at", "pk").values_list(
                    "pk", flat=True
                )[: max(0, slots)]
            )
    if batch.scope.get("mode") != "external":
        selection, parameters = _history_communication(
            batch, combined, {"time_window_minutes": 10}, history, minutes=10
        )
        _queue(batch, "conversation_link", selection, parameters)
    if not _has_model_route(batch, "conversation_link"):
        return
    # Model bounds apply to whole originals; never truncate message bodies.
    for group in _groups(
        [
            (choice, len(getattr(row, "body", "")))
            for row, choice in zip(rows, choices, strict=True)
        ],
        count=min(20, settings.PANDECT_ANALYSIS_MAX_SOURCES),
        characters=16_000,
    ):
        extras = [choice for choice in combined if choice["kind"] != kind]
        # Other message bodies have not been included in this chunk's character bound.
        extras = [choice for choice in extras if choice["kind"] in CALL_KINDS]
        selection = (
            group + extras[: max(0, settings.PANDECT_ANALYSIS_MAX_SOURCES - len(group))]
        )
        selection, parameters = _history_communication(
            batch,
            selection,
            {"semantic_links": True, "time_window_minutes": 10},
            history,
            minutes=10,
            model=True,
        )
        _model_checks(batch, "conversation_link", selection, parameters)


def prepare_reprocessing(batch, case):
    """Prepare one bounded group inside the batch cursor's transaction."""
    from core.analysis.bulk import save_source_cursor

    if batch.scope.get("analysis_version") != ANALYSIS_VERSION:
        validate_reprocessing_executions(batch)
    kind, additions, rows = _next_page(batch, case)
    if not rows:
        return False
    _prepare_sources(batch, case, kind, additions, rows)
    batch.processed_sources += len(rows)
    save_source_cursor(batch, rows[-1], _source_time_field(kind, additions))
    return True


def prepare_added_evidence(batch, case):
    kind = batch.scope["kind"]
    evidence_id = batch.scope["id"]
    attachment_id = batch.scope.get("attachment_id")
    try:
        original = get_evidence(case, kind, evidence_id)
        if original.is_archived and not batch.scope.get("reprocess"):
            raise Http404
        if attachment_id:
            attachment = original.attachments.filter(
                case=case, pk=attachment_id
            ).first()
            if attachment is None:
                raise Http404
    except Http404:
        _error(batch, kind, evidence_id, "source_unavailable")
    else:
        batch.source_kind = kind
        if batch.scope.get("reprocess") and kind in CALL_KINDS:
            if not batch.source_cursor:
                _prepare_sources(batch, case, kind, False, [original])
                batch.processed_sources = 1
            additions = original.attachments.filter(
                case=case, created_at__lte=batch.created_at
            )
            if batch.source_cursor:
                additions = additions.filter(pk__gt=batch.source_cursor)
            rows = list(
                additions.order_by("pk")[
                    : min(25, settings.PANDECT_ANALYSIS_MAX_SOURCES)
                ]
            )
            if rows:
                _prepare_sources(batch, case, kind, True, rows)
                batch.processed_sources += len(rows)
                batch.source_cursor = rows[-1].pk
            return bool(rows)
        _prepare_sources(
            batch,
            case,
            kind,
            bool(attachment_id),
            [attachment] if attachment_id else [original],
        )
        if attachment_id and batch.scope.get("new_call"):
            _prepare_sources(batch, case, kind, False, [original])
    batch.processed_sources = 1
    batch.source_cursor = attachment_id or evidence_id
    return False


def prepare_vehicle_import(batch, case):
    from core.analysis.bulk import retained_cursor_time, save_source_cursor
    from core.analysis.import_checks import imported_sources

    kind, observations = imported_sources(case, batch.scope)
    observations = observations.filter(is_archived=False)
    if batch.source_cursor:
        vehicle_id = batch.scope["_cursor"]["vehicle_id"]
        moment = retained_cursor_time(batch)
        if moment is None:
            remaining = Q(observed_at__isnull=True, pk__gt=batch.source_cursor)
        else:
            remaining = (
                Q(observed_at__gt=moment)
                | Q(observed_at=moment, pk__gt=batch.source_cursor)
                | Q(observed_at__isnull=True)
            )
        observations = observations.filter(
            Q(vehicle_id__gt=vehicle_id) | Q(remaining, vehicle_id=vehicle_id)
        )
    limit = min(25, settings.PANDECT_ANALYSIS_MAX_SOURCES)
    rows = list(
        observations.order_by(
            "vehicle_id", F("observed_at").asc(nulls_last=True), "pk"
        )[:limit]
    )
    if not rows:
        return False
    batch.source_kind = kind
    _prepare_sources(batch, case, kind, False, rows)
    batch.processed_sources += len(rows)
    save_source_cursor(
        batch, rows[-1], "observed_at", vehicle_id=str(rows[-1].vehicle_id)
    )
    return True


def _prepare_sources(batch, case, kind, additions, rows):
    versioned = batch.scope.get("analysis_version") == ANALYSIS_VERSION
    local_checks = batch.scope.get("mode") != "external"
    valid, choices, entries = [], [], []
    text_rows = {}
    for row in rows:
        item = (
            getattr(row, "phone_call" if kind == "phone-call" else "facetime")
            if additions
            else row
        )
        choice = {"kind": kind, "id": str(item.pk)}
        if additions:
            choice["attachment_id"] = str(row.pk)
        try:
            snapshot_sources(case, [choice])
        except (AnalysisError, ValidationError, Http404) as error:
            _error(
                batch,
                kind,
                item.pk,
                error.code
                if isinstance(error, AnalysisError)
                else "source_unavailable",
            )
            continue
        valid.append(row)
        choices.append(choice)
        text = row.transcript if additions else getattr(row, "body", "")
        if text.strip():
            entries.append((choice, len(text)))
            text_rows[source_id(choice)] = (item, text)
        if kind == "document":
            operation = "native_text"
            if versioned and row.file:
                try:
                    with row.file.open("rb") as original:
                        header = original.read(8)
                    if header.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")):
                        operation = "ocr"
                except OSError:
                    _error(
                        batch, kind, row.pk, "source_unavailable", operation=operation
                    )
                    continue
            _queue(batch, operation, [choice], {"automatic_followup": True})
        elif kind == "image":
            if local_checks:
                _queue(batch, "image_metadata", [choice])
                _queue(batch, "plate_ocr", [choice])
            _queue(batch, "ocr", [choice], {"automatic_followup": True})
        elif kind == "audio" or (additions and row.file):
            if local_checks:
                _queue(batch, "audio_metadata", [choice])
            parameters = {"automatic_followup": True}
            if versioned:
                identity = f"transcript:{kind}:{item.pk}:{row.pk if additions else ''}"
                parameters["_transcript_group"] = str(uuid5(batch.pk, identity))
            _model_checks(
                batch,
                "transcription",
                [choice],
                parameters,
            )
    if entries:
        _text_checks(
            batch,
            entries,
            writing=kind in {"text", "text-message"},
            messages=kind == "text-message",
            communications=additions,
            history=(case, text_rows) if batch.scope["type"] == "added" else None,
        )
    if kind in COMMUNICATION_KINDS and not additions:
        _communication_checks(batch, case, kind, valid, choices)
    if kind in LOCATION_KINDS:
        if local_checks:
            for operation in (
                "location_quality",
                "normalize_addresses",
                "spatial_groups",
                "candidate_visits",
                "candidate_trips",
                "movement_routines",
                "colocation",
                "travel_discrepancies",
            ):
                selection, parameters = choices, {}
                if batch.scope["type"] in {"added", "vehicle"} and operation not in {
                    "location_quality",
                    "normalize_addresses",
                }:
                    selection = location_selection(
                        case, batch, kind, choices, valid, operation
                    )
                    parameters = comparison_parameters(choices, selection, {})
                _queue(batch, operation, selection, parameters)
        addresses = choices
        positions = choices
        if versioned:
            addresses = [
                choice
                for row, choice in zip(valid, choices, strict=True)
                if row.address.strip()
            ]
            positions = [
                choice
                for row, choice in zip(valid, choices, strict=True)
                if row.latitude is not None and row.longitude is not None
            ]
            route = batch.scope["executions"].get("geocode_locations")
            if (
                route
                and route["location"] == "local"
                and batch.scope["type"] != "reprocess"
                and not batch.scope.get("reprocess")
            ):
                from geo.map_geocoding import unused_local_forward_choices

                addresses = unused_local_forward_choices(case, kind, addresses, route)
        _queue(batch, "geocode_locations", addresses)
        _queue(batch, "geographic_context", positions)
        _queue(batch, "geocode_locations", positions, {"reverse": True})


def follow_up_reprocessing(run, result):
    """Keep extraction published when a follow-up cannot be prepared."""
    batch = CaseAnalysisBatch.objects.select_related("requested_by").get(
        pk=run.batch_id
    )
    failure = ""
    try:
        case = get_case(batch.requested_by, batch.case_id, edit=True, lock=True)
        if not EmailAddress.objects.filter(
            user=batch.requested_by, verified=True
        ).exists():
            raise PermissionDenied
        if batch.scope.get("analysis_version") != ANALYSIS_VERSION:
            validate_reprocessing_executions(batch)
    except (AnalysisError, Http404, PermissionDenied) as error:
        failure = error.code if isinstance(error, AnalysisError) else "access_removed"
    # Preparation locks the case before the batch; use the same order here.
    batch = CaseAnalysisBatch.objects.select_for_update().get(pk=run.batch_id)
    if failure:
        _error(
            batch,
            run.inputs[0]["kind"],
            run.inputs[0]["evidence_id"],
            failure,
        )
    else:
        entries = []
        history = {}
        for source in run.inputs:
            text = "\n".join(
                piece.get("text", "")
                for piece in result.data.get("items", [])
                if source["id"] in piece.get("source_ids", [])
            )
            if text.strip():
                choice = {
                    "kind": source["kind"],
                    "id": source["evidence_id"],
                    "result_id": str(result.pk),
                }
                if source.get("attachment_id"):
                    choice["attachment_id"] = source["attachment_id"]
                entries.append((choice, len(text)))
                if batch.scope["type"] == "added":
                    item = get_evidence(case, source["kind"], source["evidence_id"])
                    history[source_id(choice)] = (item, text)
        if entries and result.data.get("outcome", "complete") in {
            "complete",
            "partial",
        }:
            _text_checks(
                batch,
                entries,
                marker=str(result.pk),
                previous_run=run,
                communications=any(
                    source["kind"] in CALL_KINDS for source in run.inputs
                ),
                history=(case, history) if history else None,
            )
    batch.save(
        update_fields=["queued_runs", "preparation_errors", "unavailable_sources"]
    )


def describe_reprocessing_selection(data, run):
    scope = run.parameters.get("_reprocessing")
    if scope is None:
        return
    data.setdefault("metrics", {})["reprocessing_selection"] = scope
    if "_comparison" not in run.parameters:
        data.setdefault("warnings", []).append(
            "This run covers only its retained selection. Comparisons do not span "
            "separate groups; a group boundary can cut an exchange or journey."
        )
