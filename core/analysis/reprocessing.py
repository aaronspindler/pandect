"""Deliberate, bounded reprocessing with retained processing-location approval."""

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
    processing_external,
    requires_inference,
    retry_execution_confirmation,
    snapshot_execution,
)
from core.analysis.services import snapshot_sources, start_run
from core.evidence.queries import scoped_evidence
from core.evidence.types import CALL_KINDS, COMMUNICATION_KINDS, KINDS, LOCATION_KINDS
from core.models import CaseAnalysisBatch
from phone.models import CallEvidenceAttachment

CONTEXT_REQUIRED_OPERATIONS = (
    "question_synthesis",
    "style_change",
    "proximity",
    "location_conflicts",
    "conversation_import",
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


def _route_keys(kinds, mode):
    operations = set()
    if set(kinds) & {"audio", *CALL_KINDS}:
        operations.add("transcription")
    if set(kinds) - set(LOCATION_KINDS):
        operations.update(("text_topics", "content_events"))
    if "text-message" in kinds:
        operations.add("session_summaries")
    if set(kinds) & set(COMMUNICATION_KINDS):
        operations.add("conversation_link")
    backends = (
        ("local", "gateway")
        if mode == "both"
        else ("local" if mode == "local" else "gateway",)
    )
    keys = {
        f"{operation}:{backend}" for operation in operations for backend in backends
    }
    if set(kinds) & set(LOCATION_KINDS):
        for operation in ("geocode_locations", "geographic_context"):
            external = processing_external(operation, {})
            if mode == "both" or external == (mode == "external"):
                keys.add(operation)
    return keys


def validate_reprocessing_executions(batch):
    current = reprocessing_executions()
    if any(
        current.get(key) != value for key, value in batch.scope["executions"].items()
    ):
        raise AnalysisError("execution_changed")


@transaction.atomic
def start_reprocessing(
    actor, case_id, *, submission_id, kinds, mode, executions, external_processing=False
):
    """The caller authenticates the signed, actor-bound reviewed executions."""
    case = get_case(actor, case_id, edit=True, lock=True)
    if not isinstance(kinds, (list, tuple)) or not kinds or set(kinds) - KINDS.keys():
        raise ValidationError("Choose at least one supported evidence type.")
    if mode not in {"local", "external", "both"}:
        raise ValidationError("Choose local, external, or both processing locations.")
    if mode in {"external", "both"} and not external_processing:
        raise ValidationError("Approve external processing of the selected evidence.")
    keys = _route_keys(kinds, mode)
    current = reprocessing_executions()
    if not isinstance(executions, dict) or any(
        executions.get(key) != current[key] for key in keys
    ):
        raise ValidationError(
            "Processing configuration changed. Review the processing locations again."
        )
    scope = {
        "type": "reprocess",
        "kinds": sorted(set(kinds)),
        "mode": mode,
        "executions": {key: current[key] for key in sorted(keys)},
        "external_processing": bool(external_processing and mode != "local"),
    }
    from core.analysis.bulk import start_case_analysis

    return start_case_analysis(actor, case.pk, submission_id=submission_id, scope=scope)


def _originals(batch, case, kind):
    return scoped_evidence(case, kind).filter(
        is_archived=False, created_at__lte=batch.created_at
    )


def _next_page(batch, case):
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
            time_field = "created_at"
        else:
            queryset = originals
            time_field = (
                "occurred_at"
                if kind in COMMUNICATION_KINDS
                else "observed_at"
                if kind in LOCATION_KINDS
                else "created_at"
            )
        if phase == batch.source_kind and batch.source_cursor:
            model = CallEvidenceAttachment if additions else KINDS[kind]
            previous = model.objects.filter(case=case, pk=batch.source_cursor).first()
            if previous is None:
                raise AnalysisError("source_unavailable")
            moment = getattr(previous, time_field)
            if moment is None:
                queryset = queryset.filter(
                    **{f"{time_field}__isnull": True, "pk__gt": previous.pk}
                )
            else:
                queryset = queryset.filter(
                    Q(**{f"{time_field}__gt": moment})
                    | Q(**{time_field: moment, "pk__gt": previous.pk})
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
    if purpose or operation in {"geocode_locations", "geographic_context"}:
        if key not in batch.scope["executions"]:
            return
        if snapshot_execution(operation, parameters) != batch.scope["executions"][key]:
            raise AnalysisError("execution_changed")
    parameters = parameters | {
        "_reprocessing": {
            "batch_id": str(batch.pk),
            "selected_sources": len(choices),
            "bounded_comparison": True,
        }
    }
    submission_id = uuid5(
        batch.pk,
        f"{batch.source_kind}:{operation}:{choices[0]}:{marker}:"
        f"{parameters.get('backend', '')}:{parameters.get('reverse', '')}",
    )
    # A retained parent links exact extraction provenance. Its external follow-up
    # uses the batch's already reviewed route, rather than a new retry destination.
    confirmation = ""
    if previous_run and processing_external(
        operation, parameters, parameters.get("backend", "local")
    ):
        confirmation = retry_execution_confirmation(
            batch.requested_by_id,
            previous_run.pk,
            submission_id,
            batch.scope["executions"][key],
        )
    try:
        _, created = start_run(
            batch.requested_by,
            batch.case_id,
            operation,
            choices,
            parameters=parameters,
            submission_id=submission_id,
            external_processing=batch.scope["external_processing"],
            batch=batch,
            previous_run=previous_run,
            retry_confirmation=confirmation,
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
    for backend in ("local", "gateway"):
        _queue(
            batch,
            operation,
            choices,
            (parameters or {}) | {"backend": backend},
            marker=marker,
            previous_run=previous_run,
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
):
    queue = partial(_queue, batch, marker=marker, previous_run=previous_run)
    models = partial(_model_checks, batch, marker=marker, previous_run=previous_run)
    limit = settings.PANDECT_ANALYSIS_MAX_SOURCES
    for choices in _groups(entries, count=limit, characters=1_000_000):
        if batch.scope["mode"] != "external":
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
                queue("sessions", choices, {"gap_minutes": 60})
                queue("comm_patterns", choices, {"gap_minutes": 60})
    if batch.scope["mode"] != "external":
        for choices in _groups(entries, count=min(50, limit), characters=100_000):
            queue("copied_text", choices)
    for choices in _groups(entries, count=min(20, limit), characters=16_000):
        models("text_topics", choices, {"include_summary": True})
        models("content_events", choices)
        if messages:
            models("session_summaries", choices, {"gap_minutes": 60})
        if communications:
            if batch.scope["mode"] != "external":
                queue("conversation_link", choices, {"time_window_minutes": 10})
            models(
                "conversation_link",
                choices,
                {"semantic_links": True, "time_window_minutes": 10},
            )


def _communication_checks(batch, case, kind, rows, choices):
    combined = list(choices)
    timed = [row for row in rows if row.occurred_at is not None]
    if timed:
        for other_kind in batch.scope["kinds"]:
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
    if batch.scope["mode"] != "external":
        _queue(batch, "conversation_link", combined, {"time_window_minutes": 10})
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
        _model_checks(
            batch,
            "conversation_link",
            group
            + extras[: max(0, settings.PANDECT_ANALYSIS_MAX_SOURCES - len(group))],
            {"semantic_links": True, "time_window_minutes": 10},
        )


def prepare_reprocessing(batch, case):
    """Prepare one bounded group inside the batch cursor's transaction."""
    validate_reprocessing_executions(batch)
    kind, additions, rows = _next_page(batch, case)
    if not rows:
        return False
    valid, choices, entries = [], [], []
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
        if kind == "document":
            _queue(batch, "native_text", [choice], {"automatic_followup": True})
        elif kind == "image":
            if batch.scope["mode"] != "external":
                _queue(batch, "image_metadata", [choice])
                _queue(batch, "plate_ocr", [choice])
            _queue(batch, "ocr", [choice], {"automatic_followup": True})
        elif kind == "audio" or (additions and row.file):
            if batch.scope["mode"] != "external":
                _queue(batch, "audio_metadata", [choice])
            _model_checks(
                batch, "transcription", [choice], {"automatic_followup": True}
            )
    if entries:
        _text_checks(
            batch,
            entries,
            writing=kind in {"text", "text-message"},
            messages=kind == "text-message",
            communications=additions,
        )
    if kind in COMMUNICATION_KINDS and not additions:
        _communication_checks(batch, case, kind, valid, choices)
    if kind in LOCATION_KINDS:
        if batch.scope["mode"] != "external":
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
                _queue(batch, operation, choices)
        for operation in ("geocode_locations", "geographic_context"):
            _queue(batch, operation, choices)
        _queue(batch, "geocode_locations", choices, {"reverse": True})
    batch.processed_sources += len(rows)
    batch.source_cursor = rows[-1].pk
    return True


def follow_up_reprocessing(run, result):
    """Keep extraction published when a follow-up cannot be prepared."""
    batch = CaseAnalysisBatch.objects.select_related("requested_by").get(
        pk=run.batch_id
    )
    failure = ""
    try:
        get_case(batch.requested_by, batch.case_id, edit=True, lock=True)
        if not EmailAddress.objects.filter(
            user=batch.requested_by, verified=True
        ).exists():
            raise PermissionDenied
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
            )
    batch.save(
        update_fields=["queued_runs", "preparation_errors", "unavailable_sources"]
    )


def describe_reprocessing_selection(data, run):
    scope = run.parameters.get("_reprocessing")
    if scope is None:
        return
    data.setdefault("metrics", {})["reprocessing_selection"] = scope
    data.setdefault("warnings", []).append(
        "This run covers only its retained selection. Comparisons do not span "
        "separate groups; a group boundary can cut an exchange or journey."
    )
