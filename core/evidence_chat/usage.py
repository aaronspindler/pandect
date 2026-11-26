"""Account for the original question and the answer linked to an approved check."""

from decimal import Decimal

from django.db.models import Q

from core.models import ChatAnalysisProposal


def _stage(label, entries):
    reported = reserved = 0
    for entry in entries:
        for actual, bound in (
            ("input_tokens", "input_bound"),
            ("output_tokens", "output_limit"),
        ):
            if actual in entry:
                reported += entry[actual]
            else:
                reserved += entry[bound]
    estimates = [
        Decimal(entry["estimated_usd"]) for entry in entries if "estimated_usd" in entry
    ]
    return {
        "label": label,
        "estimated_usd": format(sum(estimates), ".6f") if estimates else "",
        "unpriced": len(entries) - len(estimates),
        "requests": len(entries),
        "bytes": sum(entry["bytes"] for entry in entries),
        "reported": reported,
        "reserved": reserved,
    }


def interaction_usage(turn):
    if turn.plan.get("agent_version"):
        return [_stage("Cloudflare investigation", turn.model_usage)]
    proposal = (
        ChatAnalysisProposal.objects.filter(
            Q(parent_id=turn.pk) | Q(continuation_id=turn.pk),
            parent__case_id=turn.case_id,
            parent__chat_id=turn.chat_id,
            run__case_id=turn.case_id,
        )
        .filter(
            Q(continuation__isnull=True)
            | Q(continuation__case_id=turn.case_id, continuation__chat_id=turn.chat_id)
        )
        .values("parent__model_usage", "continuation__model_usage")
        .first()
    )
    if proposal is None:
        return None
    # Pre-cutover linked answers retain their original usage breakdown.
    rows = [
        _stage("Original question", proposal["parent__model_usage"]),
        _stage("Local PDF extraction", []),
        _stage("Linked answer", proposal["continuation__model_usage"] or []),
    ]
    rows.append(
        {"label": "Total so far"}
        | {
            key: sum(row[key] for row in rows)
            for key in ("requests", "bytes", "reported", "reserved")
        }
    )
    return rows
