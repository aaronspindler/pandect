"""Create and inspect case-shared chat turns using current case permissions."""

from uuid import UUID, uuid4

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Q
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone

from cases.permissions import get_case, require_verified_email
from core.analysis.catalogue import archived_finding_sources
from core.analysis.errors import AnalysisError
from core.evidence.queries import scoped_evidence
from core.evidence.types import KINDS
from core.models import AnalysisResult, ChatMatch, ChatTurn, EvidenceChat

from . import ai, checkpoints, corpus, decisions_approval, retrieval_route
from .search_scope import review_restart_code


def _edit_case(actor, case_id):
    case = get_case(actor, case_id, edit=True, lock=True)
    require_verified_email(actor, "Verify your email before asking a case question.")
    return case


def submission_route(case, *, turn=None):
    execution, revision = ai.turn_execution(case)
    search = retrieval_route.hosted_scope(case)
    if turn and turn.plan.get("hosted_search") != search:
        search = {}
    from core.case_search.rerank import rerank_scope

    ranking = rerank_scope(case)
    if turn and turn.plan.get("rerank_scope") != ranking:
        ranking = {}
    routing = decisions_approval.scope(case)
    if turn and turn.plan.get("decisions_scope") != routing:
        routing = {}
    return execution, revision, search, ranking, routing


def _route(case, expected, *, turn=None):
    selected = submission_route(case, turn=turn)
    if expected is not None and selected != expected:
        raise AnalysisError("execution_changed")
    return selected


def _require_idle(chat):
    if chat.turns.filter(status__in=("queued", "running")).exists():
        raise ValidationError("Wait for the current reply before asking a follow-up.")


@transaction.atomic
def submit_turn(
    actor,
    case_id,
    question,
    *,
    submission_id,
    chat_id=None,
    expected_route=None,
):
    case = _edit_case(actor, case_id)
    if not isinstance(question, str) or not 0 < len(question.strip()) <= 2000:
        raise ValidationError("Enter a question of up to 2,000 characters.")
    question = question.strip()
    try:
        submission_id = UUID(str(submission_id))
    except ValueError, TypeError:
        raise ValidationError("The submission reference is invalid.") from None
    submissions = ChatTurn.objects.filter(
        case=case, asked_by=actor, submission_id=submission_id
    )
    existing = submissions.filter(chat__case=case).first()
    if existing:
        correct_chat = (
            str(existing.chat_id) == str(chat_id) if chat_id else existing.ordinal == 1
        )
        if correct_chat and existing.question == question:
            return existing
        raise ValidationError("This submission reference has already been used.")
    execution, revision, search, ranking, routing = _route(case, expected_route)
    if chat_id:
        chat = get_object_or_404(EvidenceChat, pk=chat_id, case=case)
        _require_idle(chat)
        previous = (
            chat.turns.order_by("-ordinal").values_list("ordinal", flat=True).first()
        )
        ordinal = (previous or 0) + 1
    else:
        chat = EvidenceChat.objects.create(
            case=case, created_by=actor, title=" ".join(question.split())[:160]
        )
        ordinal = 1
    try:
        with transaction.atomic():
            turn = ChatTurn.objects.create(
                case=case,
                chat=chat,
                ordinal=ordinal,
                asked_by=actor,
                question=question,
                submission_id=submission_id,
                execution=execution,
                plan={
                    "timezone": timezone.get_current_timezone_name(),
                    "hosted_search": search,
                    "rerank_scope": ranking,
                    "decisions_scope": routing,
                },
                approval_revision=revision,
            )
    except IntegrityError:
        if submissions.exists():
            raise ValidationError(
                "This submission reference has already been used."
            ) from None
        raise
    checkpoints.enqueue_turn(turn)
    return turn


@transaction.atomic
def retry_turn(actor, case_id, chat_id, turn_id, *, expected_route=None):
    case = _edit_case(actor, case_id)
    turn = get_object_or_404(
        ChatTurn.objects.select_for_update(of=("self",)),
        pk=turn_id,
        case=case,
        chat_id=chat_id,
        chat__case=case,
    )
    if turn.status != "failed":
        return turn
    if turn.include_archived:
        raise ValidationError("Ask a new question to use active evidence.")
    if code := review_restart_code(turn):
        raise AnalysisError(code)
    if turn.chat.turns.filter(ordinal__gt=turn.ordinal).exists():
        raise ValidationError("Ask the question again at the end of this chat.")
    _require_idle(turn.chat)
    if _route(case, expected_route, turn=turn)[:2] != (
        turn.execution,
        turn.approval_revision,
    ):
        raise ValidationError(
            "AI settings changed. Ask a new question using the current settings."
        )
    # The worker continues under the person who asked the question. Revoked
    # requesters can never be revived by a collaborator retrying a delivery.
    try:
        _edit_case(turn.asked_by, case.pk)
    except Http404, PermissionDenied:
        raise ValidationError(
            "The original investigator has lost access. Ask a new question."
        ) from None
    turn.status = "queued"
    turn.delivery_token = uuid4()
    turn.error_code = ""
    turn.completed_at = None
    turn.save(update_fields=["status", "delivery_token", "error_code", "completed_at"])
    checkpoints.enqueue_turn(turn)
    return turn


@transaction.atomic
def recover_interrupted_delivery(turn_id, reviewed_token):
    """Replace a confirmed delivery under the original requester's current access."""
    turn = ChatTurn.objects.select_related("asked_by").get(pk=turn_id)
    case = _edit_case(turn.asked_by, turn.case_id)
    turn = ChatTurn.objects.select_for_update().get(
        pk=turn_id, case=case, chat__case=case
    )
    if (
        turn.status not in {ChatTurn.Status.QUEUED, ChatTurn.Status.RUNNING}
        or turn.delivery_token != reviewed_token
    ):
        raise AnalysisError("chat_delivery_changed")
    ai.check_execution(turn, case)
    turn.delivery_token = uuid4()
    turn.save(update_fields=["delivery_token"])
    checkpoints.enqueue_turn(turn)


def _citation(case, source, ref, *, read=None):
    if not isinstance(ref, dict) or source is None:
        return None
    start, end, quote = ref.get("start"), ref.get("end"), ref.get("quote")
    if (
        type(start) is not int
        or start < 0
        or not isinstance(quote, str)
        or not quote
        or end != start + len(quote)
    ):
        return None
    return corpus.cite(case, source.locator, start, quote, read=read)


def inspect_turn(turn, *, read=None):
    """Resolve source links against active evidence without changing saved replies."""
    from .history import recalled_sources_available

    if turn.status != "complete":
        return {}
    if turn.chat.case_id != turn.case_id or review_restart_code(turn):
        return None
    if not recalled_sources_available(turn, read or corpus.read_window):
        return None
    if "recorded_selection" in turn.plan:
        from . import recorded

        try:
            if not recorded.available(turn):
                return None
        except AnalysisError as exc:
            if exc.code != "chat_budget_exhausted":
                raise
            return None
    if turn.mode == "full":
        # Aggregates depend on misses as well as matches. Check membership in
        # PostgreSQL so reading a total need not fetch every frozen passage.
        originals = Q(
            evidence_kind="finding", evidence_id__isnull=True, result_id__isnull=False
        )
        for kind in KINDS:
            originals |= Q(
                evidence_kind=kind,
                evidence_id__in=scoped_evidence(turn.case, kind)
                .filter(is_archived=False)
                .values("pk"),
            )
        results = AnalysisResult.objects.filter(case=turn.case, run__case=turn.case)
        available = (
            Q(case_id=turn.case_id)
            & originals
            & (Q(result_id__isnull=True) | Q(result_id__in=results.values("pk")))
        )
        archived_dependencies = archived_finding_sources(turn.case).filter(
            finding__result_id=OuterRef("result_id")
        )
        manifest = turn.sources.exclude(status="context")
        if manifest.exclude(available & ~Exists(archived_dependencies)).exists():
            return None
    if run_id := turn.plan.get("saved_extraction"):
        if not AnalysisResult.objects.filter(
            case=turn.case,
            run_id=run_id,
            run__case=turn.case,
            run__operation="native_text",
        ).exists():
            return None
    answer = turn.answer
    resolved = []
    claims = answer.get("claims", [])
    keys = {ref["key"] for claim in claims for ref in claim["citations"]}
    sources = {
        source.key: source
        for source in turn.sources.filter(case_id=turn.case_id, key__in=keys)
    }
    # Clarifications and suggested next checks can draw on an excerpt even
    # when no claim quotes it. Withhold them if that context becomes invalid.
    for source in turn.sources.filter(status="context").exclude(key__in=keys):
        if (
            source.case_id != turn.case_id
            or (read or corpus.read_window)(turn.case, source.locator, 0, 1) is None
        ):
            return None
    for claim in claims:
        citations = [
            _citation(turn.case, sources.get(ref["key"]), ref, read=read)
            for ref in claim["citations"]
        ]
        if not citations or any(c is None for c in citations):
            return None
        resolved.append({"text": claim["text"], "citations": citations})
    return {
        "claims": resolved,
        "summary": answer.get("summary", ""),
        "clarification": answer.get("clarification", ""),
        "lead": answer.get("lead", ""),
    }


def reply_available(turn):
    return turn.status == "complete" and inspect_turn(turn) is not None


def match_page(turn, page):
    hits = (
        ChatMatch.objects.filter(
            case_id=turn.case_id,
            turn=turn,
            source__case_id=turn.case_id,
            source__turn=turn,
            turn__chat__case_id=turn.case_id,
            turn__status="complete",
        )
        .select_related("source")
        .order_by("source__record_key", "source__key", "start", "end")
    )
    result = Paginator(hits, 25).get_page(page)
    rows = []
    for hit in result:
        citation = _citation(
            turn.case,
            hit.source,
            {"start": hit.start, "end": hit.end, "quote": hit.quote},
        )
        rows.append(
            {
                "record_key": hit.source.record_key,
                "citations": [citation] if citation else [],
            }
        )
    result.object_list = rows
    return result


def gap_page(turn, page):
    sources = (
        turn.sources.filter(
            case_id=turn.case_id,
            turn__chat__case_id=turn.case_id,
            turn__status="complete",
        )
        .exclude(gap="")
        .order_by("record_key", "key")
    )
    result = Paginator(sources, 25).get_page(page)
    rows = []
    for source in result:
        window = corpus.read_window(turn.case, source.locator, 0, 1)
        rows.append(
            {
                "label": window["label"] if window else "Unavailable source",
                "url": window["url"] if window else "",
                "gap": source.gap,
            }
        )
    result.object_list = rows
    return result
