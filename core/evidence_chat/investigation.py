"""The turn-scoped tools available to the Cloudflare investigation Agent."""

import json
from hashlib import sha256

from django.db import transaction
from django.db.models import Count, Q

from core.analysis.errors import AnalysisError
from core.analysis.inference_payloads import encode_json
from core.models import AnalysisRun

from . import activity, corpus, engine
from .checkpoints import authorize_turn, complete_turn, continue_turn, store_context
from .history import reply_context
from .search_scope import context_source_allowed, question_filters, review_population

TOOLS = (
    "search",
    "follow",
    "reports",
    "leads",
    "open",
    "full_review",
    "propose_extraction",
    "answer",
)
DECISION = engine._object(
    action={"enum": list(TOOLS)},
    query=engine.TEXT,
    source={"type": "integer"},
    offset=engine.INTEGER,
    **engine.ANSWER["properties"],
)
MAX_STEPS = 5
MAX_WINDOWS = 10
WINDOW = 1200


def processing_coverage(case):
    from core.case_search.indexing import search_index_status

    statuses = (
        AnalysisRun.objects.filter(
            case=case, status__in=("queued", "running", "failed", "partial")
        )
        .order_by()
        .values("status")
        .annotate(total=Count("pk"))
    )
    coverage = {row["status"]: row["total"] for row in statuses}
    index_status = search_index_status(case, count_passages=False)["status"]
    if index_status in ("unprepared", "queued", "failed"):
        coverage["search_index"] = index_status
    return coverage


def relevant_gaps(turn, windows):
    from core.analysis.operations import OPERATIONS

    inputs = {
        (dep["kind"], dep["id"])
        for w in windows
        for dep in w.locator.get("dependencies", ())
    }
    if not inputs or not turn.plan.get("coverage"):
        return []
    sources = Q(pk__in=[])
    for kind, key in sorted(inputs)[:64]:
        sources |= Q(inputs__contains=[{"kind": kind, "evidence_id": key}])
    rows = (
        AnalysisRun.objects.filter(
            sources, case_id=turn.case_id, created_at__lte=turn.started_at
        )
        .filter(
            Q(finished_at__gt=turn.started_at)
            | Q(status__in=("queued", "running", "failed", "partial"))
        )
        .order_by("-created_at", "pk")
        .values("pk", "operation", "status", "finished_at")[:8]
    )
    return [
        {
            "run": str(row["pk"]),
            "method": OPERATIONS[row["operation"]].label
            if row["operation"] in OPERATIONS
            else row["operation"],
            "status": "pending"
            if row["finished_at"] and row["finished_at"] > turn.started_at
            else row["status"],
        }
        for row in rows
    ]


def _digest(value):
    return sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _read(turn, refs):
    sources = dict(
        turn.sources.filter(
            case_id=turn.case_id, key__in=[r[0] for r in refs]
        ).values_list("key", "locator")
    )
    windows = []
    filters = question_filters(turn)
    for key, start in refs:
        loc = sources.get(key)
        if loc is None or not context_source_allowed(turn.case, loc, filters):
            raise AnalysisError("source_changed")
        row = corpus.read_window(turn.case, loc, start, WINDOW)
        if row is None:
            raise AnalysisError("source_changed")
        windows.append(engine.Window(loc, start, row["text"]))
    return windows


def _windows(turn, pairs):
    out = []
    filters = question_filters(turn)
    for loc, start in pairs:
        if not context_source_allowed(turn.case, loc, filters):
            continue
        row = corpus.read_window(turn.case, loc, start, WINDOW)
        if row and row["text"]:
            out.append(engine.Window(loc, start, row["text"]))
    return out


def _search(turn, query):
    from core.case_search.hosted import HostedSearchError, search

    try:
        pairs = search(
            turn.case,
            turn.asked_by,
            query,
            through=turn.started_at,
            limit=6,
            filters=question_filters(turn),
            authorize_transfer=lambda: authorize_turn(turn),
            omit_record_notes=True,
        )
    except HostedSearchError as exc:
        # Partial backfills are usable. An outage or disabled grant fails closed.
        if exc.code in ("search_preparing", "search_item_failed"):
            turn.plan["search_preparing"] = True
            return []
        raise AnalysisError(exc.code) from None
    return _windows(turn, pairs)


@transaction.atomic
def _advance(turn, windows, tool):
    authorize_turn(turn, windows, lock=True)
    store_context(turn, windows)
    refs = turn.plan.get("agent_windows", [])
    added = [[w.locator["key"], w.start] for w in windows]
    turn.plan["agent_windows"] = [r for r in refs if r not in added] + added
    turn.plan["agent_windows"] = turn.plan["agent_windows"][-MAX_WINDOWS:]
    turn.plan.setdefault("agent_tools", []).append(tool)
    turn.plan.pop("agent_decision", None)
    turn.plan["relevant_gaps"] = relevant_gaps(
        turn, _read(turn, turn.plan["agent_windows"])
    )
    turn.plan["stage"] = "decide"
    activity.record(
        turn,
        {"follow": "follow_source", "leads": "follow_source", "open": "compare"}.get(
            tool, "search"
        ),
        windows,
    )
    continue_turn(turn)


def _leads(turn, case, *, source_id, term):
    from core.relationships import follow_leads

    filters = question_filters(turn)
    return [
        row
        for row in follow_leads(
            case,
            actor=turn.asked_by,
            through=turn.started_at,
            source_id=source_id,
            term=term,
            limit=4,
        )
        if context_source_allowed(case, row["locator"], filters)
    ]


def current_leads(turn, case):
    descriptions = []
    for saved in turn.plan.get("graph_checks", ()):
        leads = _leads(turn, case, source_id=saved["source_id"], term=saved["term"])
        if _digest(leads) != saved["digest"]:
            raise AnalysisError("source_changed")
        for row in leads:
            descriptions.append(
                {
                    key: row[key]
                    for key in (
                        "subject",
                        "predicate",
                        "object",
                        "status",
                        "qualification",
                        "quote",
                    )
                }
            )
    return descriptions


def _model(turn, body):
    policy = engine._string(body.get("instructions"), 2600, required=True)
    windows = _read(turn, turn.plan.get("agent_windows", []))
    history = reply_context(turn)
    choice, used = engine._request(
        turn,
        lambda w, **kw: authorize_turn(turn, w, **kw),
        policy,
        {
            "question": turn.question,
            "history": history,
            "cutoff": turn.started_at.isoformat(),
            "case_processing_at_cutoff": turn.plan.get("coverage", {}),
            "source_processing_gaps": turn.plan.get("relevant_gaps", []),
            "tool_history": turn.plan["agent_tools"],
            "lookup_steps_remaining": max(0, MAX_STEPS - len(turn.plan["agent_tools"])),
            "relationship_leads": current_leads(turn, turn.case),
            "instruction_at_limit": (
                "Answer from cited evidence and identify any unresolved gap "
                "when no lookup steps remain."
            ),
        },
        DECISION,
        windows,
    )
    if choice.get("action") not in TOOLS or len(encode_json(choice)) > 32768:
        raise AnalysisError("model_result_structure")
    with transaction.atomic():
        authorize_turn(turn, used, lock=True)
        turn.plan["agent_decision"] = _digest(choice)
        turn.plan["agent_used"] = [[w.locator["key"], w.start] for w in used]
        turn.save(update_fields=["plan"])
    return choice


def _supported(turn, result, windows):
    """Check semantic support separately from deterministic quotation verification."""
    claims = result["claims"]
    if not claims:
        return result
    # Number only the quoted evidence for each claim; prevent the checker from
    # crediting another record's statement to the source this claim cites.
    payload = {
        "claims": [
            {"text": c["text"], "quotations": [r["quote"] for r in c["citations"]]}
            for c in claims
        ]
    }
    verdicts, _ = engine._request(
        turn,
        lambda _, **kw: authorize_turn(turn, windows, **kw),
        "Return one boolean per claim in order. True only if its own cited "
        "quotations support every factual part, attribution and certainty in "
        "context. Reject inferences of identity, corroboration, or conduct "
        "from speculation, forwards or silence.",
        payload,
        engine._object(supported=engine._array({"type": "boolean"})),
        windows,
    )
    checks = verdicts.get("supported")
    if (
        not isinstance(checks, list)
        or len(checks) != len(claims)
        or any(type(v) is not bool for v in checks)
    ):
        raise AnalysisError("model_result_structure")
    result["claims"] = [c for c, ok in zip(claims, checks, strict=True) if ok]
    if not all(checks):
        result["summary"] = (
            "Claims with insufficient support in their cited passages were withheld."
        )
    return result


def _verified_answer(turn, choice, windows):
    # A bad citation withholds its claim independently of the other claims.
    claims = choice.get("claims")
    if not isinstance(claims, list) or len(claims) > 16:
        raise AnalysisError("model_result_structure")
    if not claims:
        return engine.validated_answer(choice, windows)
    result = engine.empty_answer(lead=engine._string(choice.get("lead"), 500))
    for claim in claims:
        try:
            parsed = engine.validated_answer(
                {"claims": [claim], "lead": "", "clarification": ""}, windows
            )
        except AnalysisError:
            result["summary"] = "Claims with unverifiable quotations were withheld."
        else:
            result["claims"].extend(parsed["claims"])
    return _supported(turn, result, windows)


@transaction.atomic
def _publish(turn, result, windows, reason):
    from .lineage import corroboration, forwarded_origins

    case = authorize_turn(turn, windows, lock=True)
    store_context(turn, windows)
    known = {w.locator["key"]: w.locator for w in windows}
    mark = forwarded_origins(case, known.values())
    support = []
    for claim in result["claims"]:
        refs = claim["citations"]
        support.append(
            corroboration([known[key] for key in {r["key"] for r in refs}], mark)
        )
        if any(
            not corpus.cite(case, known[r["key"]], r["start"], r["quote"]) for r in refs
        ):
            raise AnalysisError("source_changed")
    turn.plan["claim_support"] = support
    turn.plan["stop_reason"] = reason
    turn.plan["stage"] = "complete"
    turn.plan.pop("agent_decision", None)
    turn.mode = "clarification" if result["clarification"] else "ordinary"
    turn.total = turn.reviewed = turn.sources.filter(case=case).count()
    turn.gaps = turn.sources.filter(case=case).exclude(gap="").count()
    activity.record(turn, "publish")
    complete_turn(turn, result)


def _limited(turn):
    if turn.mode == "full":
        from .full_review import PENDING_REVIEW

        with transaction.atomic():
            case = authorize_turn(turn, lock=True)
            remaining = turn.sources.filter(
                case=case, status__in=PENDING_REVIEW
            ).update(
                status="unread", gap="The model usage limit interrupted this entry."
            )
            turn.gaps += remaining
            turn.plan.update(
                stage="verify",
                narrative=False,
                unreviewed=remaining,
                stop_reason="budget",
            )
            continue_turn(turn)
        return
    windows = _read(turn, turn.plan.get("agent_windows", []))
    _publish(
        turn,
        engine.empty_answer(
            "The investigation reached its usage or search limit before an answer "
            "could be verified."
        ),
        windows,
        "budget",
    )


def _review(turn):
    from . import engine_counts, full_review

    filters, clarify = engine_counts.structured_question(turn, turn.question)
    if clarify:
        _publish(turn, engine.empty_answer(clarification=clarify), [], "clarification")
        return
    literal = engine_counts.literal_question(turn)
    scope = question_filters(turn)
    if review_population(turn.question) == "text-message":
        scope["record_type"] = "text-message"
    full_review.freeze_review(
        turn,
        turn.question,
        filters or {},
        narrative=not filters and not literal,
        literal=literal,
        scope=scope,
    )


def _act(turn, decision):
    if not isinstance(decision, dict) or _digest(decision) != turn.plan.get(
        "agent_decision"
    ):
        raise AnalysisError("processing_cancelled")
    used = _read(turn, turn.plan["agent_used"])
    authorize_turn(turn, used)
    tool = decision["action"]
    if tool == "full_review":
        return _review(turn)
    if tool == "answer":
        return _publish(turn, _verified_answer(turn, decision, used), used, "answered")
    if len(turn.plan["agent_tools"]) >= MAX_STEPS:
        return _limited(turn)
    index = decision.get("source")
    selected = used[index] if type(index) is int and 0 <= index < len(used) else None
    if tool in ("open", "follow", "reports", "propose_extraction") and not selected:
        raise AnalysisError("model_citation_source")
    if tool == "open":
        offset = decision.get("offset")
        if type(offset) is not int or not 0 <= offset < selected.locator["length"]:
            raise AnalysisError("model_citation_source")
        windows = _windows(turn, [(selected.locator, offset)])
    elif tool == "follow":
        from .corpus_navigation import linked_materials

        links = linked_materials(
            turn.case,
            selected.locator,
            turn.question,
            through=turn.started_at,
            filters=question_filters(turn),
        )
        filters = question_filters(turn)
        windows = [
            engine.Window(link.locator, 0, link.text)
            for link in links
            if context_source_allowed(turn.case, link.locator, filters)
        ]
    elif tool == "reports":
        from .analysis_reports import (
            eligible_results,
            overlapping_results,
            read_analysis_material,
        )

        windows = []
        if not question_filters(turn):
            results = overlapping_results(
                eligible_results(turn.case).filter(created_at__lte=turn.started_at),
                turn.case,
                [selected.locator],
            )
            for key in results.order_by("-created_at", "pk").values_list(
                "pk", flat=True
            )[:6]:
                report = read_analysis_material(
                    turn.case, key, 0, WINDOW, through=turn.started_at
                )
                if report:
                    windows.append(engine.Window(report.locator, 0, report.text))
    elif tool == "leads":
        query = engine._string(decision.get("query"), 200)
        source_id = selected.locator.get("evidence_id") if selected else None
        leads = _leads(turn, turn.case, source_id=source_id, term=query)
        windows = _windows(turn, [(r["locator"], r["start"]) for r in leads])
        turn.plan.setdefault("graph_checks", []).append(
            {
                "source_id": source_id,
                "term": query,
                "digest": _digest(leads),
            }
        )
    elif tool == "propose_extraction":
        from .proposals import prepare_agent_proposal

        reason = engine._string(decision.get("query"), 300, required=True)
        result = _verified_answer(turn, decision, used)
        prepare_agent_proposal(turn, selected, reason, used, result)
        return _publish(turn, result, used, "processing_gap")
    else:
        query = engine._string(decision.get("query"), 300, required=True)
        windows = _search(turn, query)
    _advance(turn, windows, tool)


def step(turn, body):
    op = body["operation"]
    try:
        if op == "start":
            from . import engine_counts

            if (
                engine_counts.literal_question(turn)
                or review_population(turn.question)
                or any(engine_counts.structured_question(turn, turn.question))
            ):
                return _review(turn)
            hints = []
            history = reply_context(turn, hints=hints)
            if history:
                turn.plan["history_digest"] = sha256(encode_json(history)).hexdigest()
            windows = _windows(turn, hints[:3]) + _search(turn, turn.question)
            _advance(turn, windows, "search")
        elif op == "model":
            return _model(turn, body)
        elif op == "act":
            _act(turn, body.get("decision"))
        elif op == "approved_extraction":
            from .proposals import approved_agent_passages

            _advance(turn, approved_agent_passages(turn), "approved_extraction")
        else:
            from .full_review import review_batch, verify_batch

            if op == "literal_result":
                return review_batch(turn, literal_result=body)
            if op == "review":
                return review_batch(turn)
            verify_batch(turn)
    except AnalysisError as exc:
        if exc.code != "chat_budget_exhausted":
            raise
        _limited(turn)
        # The Agent's stale follow-up cannot publish after this terminal write.
        return {"action": "answer"}
