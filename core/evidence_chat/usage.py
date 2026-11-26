"""Account for the original question and the answer linked to an approved check."""

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
    return {
        "label": label,
        "requests": len(entries),
        "bytes": sum(entry["bytes"] for entry in entries),
        "reported": reported,
        "reserved": reserved,
    }


def interaction_usage(turn):
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
    # The approved PDF extraction uses the local parser and optional local OCR.
    # Neither operation dispatches model requests.
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
