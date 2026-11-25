"""Group complete reported pairs locally, carrying each stream across worker pages."""

from datetime import datetime
from hashlib import sha256
from json import dumps
from math import isfinite
from uuid import uuid5

from allauth.account.models import EmailAddress
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, Exists, F, OuterRef, Q
from django.utils import timezone

from cases.permissions import get_case
from core.analysis.errors import AnalysisError, error_message
from people.models import Person
from phone.conversations import pair_messages
from phone.grouping_sources import (
    checked_message,
    matching_originals,
    unique_reply_target,
)
from phone.models import (
    CommunicationContext,
    ConversationGrouping,
    ConversationGroupingRequest,
    ConversationPairRevision,
    ConversationSession,
    ConversationSessionMember,
)
from phone.session_matching import (
    REPLY_WINDOW,
    SESSION_RULES_VERSION,
    message_context,
    participant_stream,
    session_decision,
)

PAGE_SIZE = 100


def _originals(case, first, second):
    return pair_messages(case, first, second).order_by(
        F("occurred_at").asc(nulls_last=True), "pk"
    )


def _extend_scope(digest, pk, checksum, context_id):
    return sha256(
        digest
        + pk.bytes
        + bytes.fromhex(checksum)
        + (context_id.bytes if context_id else b"")
    ).digest()


def _legacy_scope(case, first, second):
    """Keep the historical ordered digest for already queued revisions."""
    digest = b""
    count = 0
    keys = _originals(case, first, second).values_list(
        "pk", "sha256", "communication_context__pk"
    )
    for pk, checksum, context_id in keys.iterator(chunk_size=1000):
        digest = _extend_scope(digest, pk, checksum, context_id)
        count += 1
    return count, digest.hex()


def _pair_revision(case, first, second):
    first_id, second_id = sorted(
        (getattr(first, "pk", first), getattr(second, "pk", second))
    )
    revision = (
        ConversationPairRevision.objects.filter(
            case=case, first_person_id=first_id, second_person_id=second_id
        )
        .values_list("revision", flat=True)
        .first()
    )
    return (revision or uuid5(case.pk, f"{first_id}:{second_id}")).hex


def grouping_scope(case, first, second):
    """Bind the form to the transactionally tracked original selection."""
    return (
        _originals(case, first, second).order_by().count(),
        _pair_revision(case, first, second),
    )


def _register(batch, case, pair, scope, gap_minutes):
    count, digest = scope
    identity = {
        "case": case,
        "first_person_id": pair[0],
        "second_person_id": pair[1],
        "parameters": {"gap_minutes": gap_minutes},
        "method_version": SESSION_RULES_VERSION,
        "source_count": count,
        "scope_digest": digest,
    }
    revision = None
    if batch.scope["type"] != "conversation_grouping":
        from phone.saved_sessions import valid_sessions

        matching = ConversationGrouping.objects.filter(**identity)
        # A reviewed source deletion can remove entire historical sessions.
        # Reuse only revisions that still retain every exact original.
        invalid_sessions = ConversationSession.objects.filter(
            grouping=OuterRef("pk")
        ).exclude(pk__in=valid_sessions(case).values("pk"))
        revision = (
            matching.filter(~Exists(invalid_sessions), published_at__isnull=False)
            .annotate(retained=Count("sessions__members"))
            .filter(retained=count)
            .order_by("-published_at", "pk")
            .first()
        )
        if revision is None:
            revision = (
                matching.filter(published_at__isnull=True)
                .order_by("batch__created_at", "pk")
                .first()
            )
    if revision is None:
        revision = ConversationGrouping.objects.create(**identity, batch=batch)
    ConversationGroupingRequest.objects.create(
        case=case, batch=batch, grouping=revision
    )
    return revision


def register_message_pairs(batch, case, messages):
    """A batch prepares each pair once, regardless of its number of source pages."""
    for pair in {
        tuple(sorted((row.sender_id, row.recipient_id)))
        for row in messages
        if row.recipient_id is not None
    }:
        if not batch.grouping_requests.filter(
            case=case,
            grouping__first_person_id=pair[0],
            grouping__second_person_id=pair[1],
        ).exists():
            _register(batch, case, pair, grouping_scope(case, *pair), 60)


@transaction.atomic
def start_grouping(
    actor, case, first, second, *, gap_minutes, reviewed_scope, submission_id
):
    from core.analysis.bulk import start_case_analysis

    case = get_case(actor, case.pk, edit=True, lock=True)
    pair = sorted((getattr(first, "pk", first), getattr(second, "pk", second)))
    if (
        type(gap_minutes) not in (int, float)
        or not isfinite(gap_minutes)
        or not 1 <= gap_minutes <= 10080
    ):
        raise ValidationError("Choose a gap between 1 and 10,080 minutes.")
    scope = grouping_scope(case, *pair)
    if not scope[0] or scope[1] != reviewed_scope:
        raise ValidationError(
            "The messages changed. Review the current coverage and submit again."
        )
    batch, created = start_case_analysis(
        actor,
        case.pk,
        submission_id=submission_id,
        scope={
            "type": "conversation_grouping",
            "analysis_version": 2,
            "people": list(map(str, pair)),
            "gap_minutes": gap_minutes,
            "source_count": scope[0],
            "reviewed_scope": reviewed_scope,
        },
    )
    if created:
        _register(batch, case, pair, scope, gap_minutes)
    return batch, created


def _native_parent(current, session, originals, page_members, case):
    parent_id = unique_reply_target(case, current)
    if parent_id is None:
        return None
    if parent_id in page_members:
        parent, parent_session = page_members[parent_id]
        return parent if parent_session == session else None
    if not session.members.filter(case=case, message_id=parent_id).exists():
        return None
    parent = originals.filter(pk=parent_id).first()
    return checked_message(parent, case) if parent else None


def _next_messages(originals, cursor):
    if cursor:
        moment = datetime.fromisoformat(cursor["time"]) if cursor["time"] else None
        later = Q(occurred_at__isnull=True, pk__gt=cursor["id"])
        if moment is not None:
            later = (
                Q(occurred_at__gt=moment)
                | Q(occurred_at=moment, pk__gt=cursor["id"])
                | Q(occurred_at__isnull=True)
            )
        originals = originals.filter(later)
    return list(originals[:PAGE_SIZE])


def _append_page(revision, case, originals, rows):
    active = {}
    page_members = {}
    digest = bytes.fromhex(revision.processed_digest)
    new_sessions, touched, members = [], {}, []
    for row in rows:
        current = checked_message(row, case)
        key = sha256(dumps(participant_stream(current)).encode()).hexdigest()
        if key not in active:
            session = (
                revision.sessions.filter(stream_key=key)
                .order_by("-start_ordinal")
                .first()
            )
            previous = None
            if session:
                previous_id = (
                    session.members.filter(ordinal=session.message_count)
                    .values_list("message_id", flat=True)
                    .get()
                )
                previous = originals.filter(pk=previous_id).first()
                if previous is None:
                    raise AnalysisError("source_changed")
                previous = checked_message(previous, case)
            active[key] = (session, previous)
        session, previous = active[key]
        parent = None
        gap = revision.parameters["gap_minutes"]
        if previous and previous.occurred_at and current.occurred_at:
            elapsed = current.occurred_at - previous.occurred_at
            if gap * 60 < elapsed.total_seconds() <= REPLY_WINDOW.total_seconds():
                parent = _native_parent(current, session, originals, page_members, case)
        decision = session_decision(
            previous, current, gap_minutes=gap, native_parent=parent
        )
        if decision.boundary:
            session = ConversationSession(
                case=case,
                grouping=revision,
                stream_key=key,
                start_ordinal=revision.processed_count + 1,
                boundary_reason=decision.boundary,
                first_at=current.occurred_at,
            )
            new_sessions.append(session)
        session.message_count += 1
        session.last_at = current.occurred_at
        session.membership_digest = sha256(
            bytes.fromhex(session.membership_digest) + current.pk.bytes
        ).hexdigest()
        touched[session.pk] = session
        context = message_context(current)
        context_id = context.pk if context else None
        members.append(
            ConversationSessionMember(
                case=case,
                session=session,
                message=current,
                ordinal=session.message_count,
                sha256=current.sha256,
                context_id_snapshot=context_id,
                continuation_reason=decision.continuation,
                parent_message_id=decision.parent.pk if decision.parent else None,
                elapsed_seconds=decision.elapsed_seconds,
            )
        )
        digest = _extend_scope(digest, current.pk, current.sha256, context_id)
        revision.processed_count += 1
        active[key] = session, current
        page_members[current.pk] = current, session
    if rows:
        revision.cursor = {
            "time": rows[-1].occurred_at.isoformat() if rows[-1].occurred_at else None,
            "id": str(rows[-1].pk),
        }
        revision.processed_digest = digest.hex()
        for session in new_sessions:
            del touched[session.pk]
        ConversationSession.objects.bulk_create(new_sessions)
        ConversationSession.objects.bulk_update(
            touched.values(), ["last_at", "message_count", "membership_digest"]
        )
        ConversationSessionMember.objects.bulk_create(members)


def _prepare_revision(revision, case, batch):
    if revision.method_version != SESSION_RULES_VERSION:
        raise AnalysisError("processing_version_unavailable")
    originals = matching_originals(
        _originals(case, revision.first_person_id, revision.second_person_id)
    )
    rows = _next_messages(originals, revision.cursor)
    _append_page(revision, case, originals, rows)
    if batch.scope["type"] == "conversation_grouping":
        batch.processed_sources = revision.processed_count
    if len(rows) < PAGE_SIZE:
        pair = (revision.first_person_id, revision.second_person_id)
        if revision.processed_count != revision.source_count:
            raise AnalysisError("grouping_scope_changed")
        if len(revision.scope_digest) == 64:
            # Deliveries queued before source revisions used a complete digest.
            if revision.processed_digest != revision.scope_digest or _legacy_scope(
                case, *pair
            ) != (revision.source_count, revision.scope_digest):
                raise AnalysisError("grouping_scope_changed")
        elif _pair_revision(
            case, *pair
        ) != revision.scope_digest or Person.objects.filter(
            case=case, pk__in=pair
        ).count() != len(set(pair)):
            raise AnalysisError("grouping_scope_changed")
        elif (
            CommunicationContext.objects.filter(
                message_id__in=originals.order_by().values("pk")
            )
            .exclude(
                case=case,
                source_document__case=case,
                source_document__sha256=F("source_sha256"),
            )
            .exists()
        ):
            raise AnalysisError("source_changed")
        actor = batch.requested_by
        actor.refresh_from_db(fields=("is_active",))
        get_case(actor, case.pk, edit=True)
        if not EmailAddress.objects.filter(user=actor, verified=True).exists():
            raise PermissionDenied
        revision.published_at = timezone.now()
        revision.published_by_id = batch.requested_by_id
    revision.save(
        update_fields=(
            "cursor",
            "processed_count",
            "processed_digest",
            "published_at",
            "published_by",
        )
    )


def pending_groupings(batch, case):
    return batch.grouping_requests.filter(
        case=case,
        grouping__case=case,
        grouping__published_at__isnull=True,
        error_code="",
    )


def prepare_grouping_step(batch, case):
    """Advance a shared exact revision under the authorized case lock."""
    pending = pending_groupings(batch, case)
    request = pending.select_related("grouping").order_by("pk").first()
    if request is None:
        return False
    revision = request.grouping
    try:
        # Discard a failed page before recording a pair-specific failure.
        with transaction.atomic():
            _prepare_revision(revision, case, batch)
    except AnalysisError as failure:
        if batch.scope["type"] == "conversation_grouping":
            raise
        request.error_code = failure.code
        request.save(update_fields=("error_code",))
        batch.unavailable_sources += 1
        if len(batch.preparation_errors) < 100:
            batch.preparation_errors.append(
                {
                    "operation": "sessions",
                    "people": [
                        str(revision.first_person_id),
                        str(revision.second_person_id),
                    ],
                    "message": error_message(failure.code),
                }
            )
    return pending.exists()
