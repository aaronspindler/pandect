"""Read published conversation sessions with their exact, ordered originals."""

from django.contrib.postgres.expressions import ArraySubquery
from django.db.models import Count, Exists, F, Func, IntegerField, OuterRef, Q, Subquery
from django.urls import reverse

from core.evidence.queries import scoped_evidence
from phone.models import ConversationSession, ConversationSessionMember
from text.analysis.shared import MAX_SOURCES


def valid_sessions(case, messages=None):
    """A changed, missing or out-of-scope original invalidates the whole grouping."""
    members = ConversationSessionMember.objects.filter(session_id=OuterRef("pk"))
    if messages is None:
        messages = scoped_evidence(case, "text-message")
    valid = (
        Q(case=case, message_id__in=messages.order_by().values("pk"))
        & Q(sha256=F("message__sha256"))
        & (
            Q(
                context_id_snapshot__isnull=True,
                message__communication_context__isnull=True,
            )
            | Q(
                context_id_snapshot=F("message__communication_context__pk"),
                message__communication_context__case=case,
                message__communication_context__source_document__case=case,
                message__communication_context__source_sha256=F(
                    "message__communication_context__source_document__sha256"
                ),
            )
        )
        & (
            Q(
                message__sender_id=OuterRef("grouping__first_person_id"),
                message__recipient_id=OuterRef("grouping__second_person_id"),
            )
            | Q(
                message__recipient_id=OuterRef("grouping__first_person_id"),
                message__sender_id=OuterRef("grouping__second_person_id"),
            )
        )
    )
    # Validate cardinality and source integrity in one pass over this session.
    complete = (
        members.order_by()
        .values("session_id")
        .annotate(
            total=Count("pk"),
            verified=Count("pk", filter=valid),
        )
        .filter(total=OuterRef("message_count"), verified=F("total"))
    )
    return ConversationSession.objects.filter(
        Exists(complete),
        case=case,
        grouping__case=case,
        grouping__batch__case=case,
        grouping__first_person__case=case,
        grouping__second_person__case=case,
        grouping__published_at__isnull=False,
        message_count__gt=0,
    )


def distinct_sessions(sessions):
    # An identical later revision retains the first valid public ID.
    representatives = sessions.order_by(
        "membership_digest", "grouping__published_at", "pk"
    ).distinct("membership_digest")
    return sessions.model.objects.filter(pk__in=Subquery(representatives.values("pk")))


def exclude_saved_copies(legacy, saved):
    """Compare exact IDs only against legacy selections, which have a fixed limit."""
    ids = (
        ConversationSessionMember.objects.filter(session_id=OuterRef("pk"))
        .order_by("message_id")
        .values("message_id")[:MAX_SOURCES]
    )
    copies = (
        saved.filter(
            message_count=Func(
                OuterRef("member_key"),
                function="cardinality",
                output_field=IntegerField(),
            )
        )
        .alias(originals=ArraySubquery(ids))
        .filter(originals=OuterRef("member_key"))
    )
    return legacy.filter(~Exists(copies))


def equivalent_saved(saved, member_ids):
    ids = ConversationSessionMember.objects.filter(session_id=OuterRef("pk"))
    return (
        saved.filter(
            ~Exists(ids.exclude(message_id__in=member_ids)),
            message_count=len(member_ids),
        )
        .select_related("grouping")
        .order_by("grouping__published_at", "pk")
        .first()
    )


def saved_row(session):
    grouping = session.grouping
    return {
        "id": session.pk,
        "saved_session": session,
        "parameters": grouping.parameters,
        "first_at": session.first_at,
        "last_at": session.last_at,
        "count": session.message_count,
        "partial": False,
        "messages": None,
        "summary": None,
        "url": reverse(
            "phone:grouping_details",
            args=(
                session.case_id,
                grouping.first_person_id,
                grouping.second_person_id,
                session.pk,
            ),
        ),
    }
