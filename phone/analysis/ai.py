"""Source-cited model suggestions across selected communications."""

from django.core.exceptions import ValidationError

from core.analysis.sources import output
from phone.analysis.deterministic.conversations import (
    VERSION,
    _ordered,
    _session_groups,
    cross_channel_links,
)
from text.analysis.ai import cited_model_analysis

SEMANTIC_FINDING_KINDS = (
    "explicit_reference",
    "shared_reported_event",
    "question_answer",
)


def semantic_cross_channel_links(sources, parameters, *, infer=None):
    "Combine observed temporal candidates with explicitly requested model suggestions."
    rows = _ordered(sources)
    result = cross_channel_links(rows, parameters)
    items, warnings, outcome = result["items"], result["warnings"], result["outcome"]
    valid_semantic = 0
    unresolved_semantic = 0
    semantic = cited_model_analysis(
        rows,
        parameters,
        infer=infer,
        finding_kinds=SEMANTIC_FINDING_KINDS,
        linked=True,
        purpose=(
            "Propose concrete explicit references, shared reported events, "
            "and question/answer links across the selected "
            "messages and identified call transcripts. Cite both sides from "
            "different originals. For explicit references and shared events, "
            "quote the same specific target from both originals and repeat it "
            "in the suggestion. Omit broad shared topics, apparent contrasts, "
            "timing alone, and unsupported references. Preserve competing "
            "referents such as 'that place' as unresolved."
        ),
    )
    for suggestion in semantic["items"]:
        if len(set(suggestion["source_ids"])) < 2:
            unresolved_semantic += 1
            suggestion["report_only"] = True
            suggestion["label"] = "Unresolved reference suggestion"
            suggestion["values"] |= {
                "status": "unresolved",
                "basis": (
                    "Only one selected source was cited; a two-sided "
                    "semantic link is unsupported"
                ),
            }
        else:
            valid_semantic += 1
        items.append(suggestion)
    warnings.extend(semantic["warnings"])
    if valid_semantic and outcome == "empty":
        outcome = "complete"
    if unresolved_semantic:
        outcome = "partial"
        warnings.append(
            "A semantic link needs exact citations to at least two "
            "distinct selected sources. One-sided suggestions "
            "remain unresolved; they are not completed links."
        )
    if semantic["outcome"] not in {"complete", "empty"}:
        outcome = "partial" if items else semantic["outcome"]
    result["outcome"] = outcome
    result["metrics"].update(
        semantic_link_candidates=valid_semantic,
        unresolved_semantic_suggestions=unresolved_semantic,
    )
    return result


def session_summaries(sources, parameters, *, infer=None):
    "Keep message/session boundaries intact when making bounded summary calls."
    messages, groups, gap = _session_groups(sources, parameters)
    if not messages:
        return output(
            "No selected messages are available to summarize.", outcome="insufficient"
        )
    if infer is None:
        return output(
            "Session summaries need a configured local or external AI runtime.",
            outcome="unsupported",
        )
    chunks = []
    for index, group in enumerate(groups, 1):
        batch = []
        size = 0
        for source in group["sources"]:
            if len(source.text) > 16_000:
                raise ValidationError(
                    "One message exceeds the 16,000-character summary chunk limit."
                )
            if batch and size + len(source.text) > 16_000:
                chunks.append((index, batch))
                batch, size = [], 0
            batch.append(source)
            size += len(source.text)
        if batch:
            chunks.append((index, batch))
    if len(chunks) > 20:
        raise ValidationError(
            "Select at most 20 conversation-aware summary chunks per run."
        )
    items = []
    outcomes = []
    warnings = [
        "Each summary covers its disclosed message chunk. "
        "Session boundaries are inferred, missing exchanges "
        "remain unknown, and model interpretations require review."
    ]
    for session, selected in chunks:
        result = cited_model_analysis(
            selected,
            parameters,
            infer=infer,
            purpose=(
                f"Summarize selected messages in inferred session {session}. "
                "State qualifications, contrary passages, plans, cancellations, "
                "unresolved "
                "questions and missing context. Cite each short summary statement; do "
                "not invent a complete-session or whole-case account."
            ),
        )
        for item in result["items"]:
            item["values"]["inferred_session"] = session
            item["values"]["chunk_source_ids"] = [str(s.id) for s in selected]
        items.extend(result["items"])
        outcomes.append(result["outcome"])
        warnings.extend(result["warnings"])
        if result["outcome"] not in {"complete", "empty"}:
            warnings.append(f"Session {session}: {result['summary']}")
    if all(outcome in {"complete", "empty"} for outcome in outcomes):
        outcome = "complete" if items else "empty"
    elif len(set(outcomes)) == 1:
        outcome = outcomes[0]
    else:
        outcome = "partial"
    return output(
        "Source-cited summaries of selected inferred sessions.",
        items=items,
        metrics={
            "selected_messages": len(messages),
            "inferred_sessions": len(groups),
            "summary_chunks": len(chunks),
            "gap_minutes": gap,
            "method": VERSION,
        },
        warnings=list(dict.fromkeys(warnings)),
        outcome=outcome,
    )
