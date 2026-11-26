"""Bind optional hosted meaning searches to the destination reviewed for a chat turn."""

import re

from core.analysis.errors import AnalysisError
from core.case_search.approval import search_approval_current
from core.case_search.parents import parent_results, routing_terms
from core.case_search.semantic import related_results
from core.case_search.semantic_model import is_external, profile


def hosted_scope(case):
    grant = case.search_embedding_approval
    chat = grant.get("chat_queries", {})
    if (
        not chat.get("enabled")
        or not chat.get("revision")
        or not search_approval_current(case)
    ):
        return {}
    return {
        "profile": grant["profile"],
        "revision": grant["revision"],
        "chat_revision": chat["revision"],
        "destination": grant["execution"],
    }


def related_for_turn(turn, query, authorize, *, filters=None):
    """Embed one bounded search and recheck turn access before each transfer."""
    words = re.findall(r"\w+", query)
    if len(words) > 16 or len(query) > 200:
        words = routing_terms(query)
    query = " ".join(words)[:200]
    if not query:
        return [], ""
    filters = filters or {}
    hosted = is_external()
    scope = turn.plan.get("hosted_search")
    case = authorize()
    if case.pk != turn.case_id:
        raise AnalysisError("access_removed")

    def local_parents():
        return parent_results(
            case, user=turn.asked_by, query=query, through=turn.started_at, **filters
        ), ""

    if not profile() or (hosted and (not scope or hosted_scope(case) != scope)):
        return local_parents()

    def check_request():
        current = authorize()
        if hosted != is_external() or (hosted and hosted_scope(current) != scope):
            raise AnalysisError("embedding_approval_required")
        return current

    try:
        return related_results(
            case,
            user=turn.asked_by,
            query=query,
            allow_external=hosted,
            include_source=True,
            authorize_request=check_request,
            chat_turn=turn,
            through=turn.started_at,
            **filters,
        )
    except AnalysisError as exc:
        # The embedding service is optional. Delivery, source and text-AI
        # authorization failures still stop the turn.
        if exc.code == "execution_changed":
            authorize()
        elif exc.code not in {
            "embedding_approval_required",
            "chat_budget_exhausted",
        } and not exc.code.startswith(("gateway_", "local_ai_")):
            raise
        return local_parents()
