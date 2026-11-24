"""Source-cited model suggestions across selected communications."""

from django.core.exceptions import ValidationError

from core.analysis.sources import output
from phone.analysis.deterministic.conversations import (
    _ordered,
    _session_groups,
    cross_channel_links,
)
from phone.session_matching import SESSION_RULES_VERSION
from text.analysis.ai import cited_model_analysis

SUMMARY_CHUNK_CHARACTERS = 16_000
MAX_SUMMARY_CHUNKS = 20

SEMANTIC_FINDING_KINDS = (
    "explicit_reference",
    "shared_reported_event",
    "question_answer",
)


def semantic_cross_channel_links(sources, parameters, *, infer=None):
    "Combine observed temporal candidates with explicitly requested model suggestions."
    rows = _ordered(sources)
    result = (
        output("AI suggestions about selected exchanges.", outcome="empty")
        if parameters.get("_suggestions_only")
        else cross_channel_links(rows, parameters)
    )
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


def summary_chunks(groups):
    """Pack ordered (message, character count) pairs without cutting a message."""
    chunks = []
    for index, group in enumerate(groups, 1):
        batch = []
        size = 0
        for message, characters in group:
            if characters > SUMMARY_CHUNK_CHARACTERS:
                raise ValidationError(
                    f"One message exceeds the {SUMMARY_CHUNK_CHARACTERS:,}-character "
                    "summary chunk limit."
                )
            if batch and size + characters > SUMMARY_CHUNK_CHARACTERS:
                chunks.append((index, batch))
                batch, size = [], 0
            batch.append(message)
            size += characters
        if batch:
            chunks.append((index, batch))
    if len(chunks) > MAX_SUMMARY_CHUNKS:
        raise ValidationError(
            f"Select at most {MAX_SUMMARY_CHUNKS} conversation-aware "
            "summary chunks per run."
        )
    return chunks


def session_summaries(sources, parameters, *, infer=None):
    "Keep message/session boundaries intact when making bounded summary calls."
    saved_range = parameters.get("_saved_session_range")
    if saved_range is None:
        messages, groups, gap = _session_groups(sources, parameters)
    else:
        # The worker verifies this range against the published session before inference.
        # Its first message can rely on a reply target held in a preceding range.
        messages = _ordered(sources, messages_only=True)
        groups = [{"sources": messages}]
        gap = parameters.get("gap_minutes")
    if not messages:
        return output(
            "No selected messages are available to summarize.",
            outcome="insufficient",
            metrics={"range_covered": False} if saved_range is not None else {},
        )
    if infer is None:
        return output(
            "Session summaries need a configured local or external AI runtime.",
            outcome="unsupported",
            metrics={"range_covered": False} if saved_range is not None else {},
        )
    chunks = summary_chunks(
        [(source, len(source.text)) for source in group["sources"]] for group in groups
    )
    if comparison := parameters.get("_comparison"):
        anchors = set(comparison["anchors"])
        chunks = [
            (session, selected)
            for session, selected in chunks
            if any(str(source.id) in anchors for source in selected)
        ]
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
        usable_ids = [str(source.id) for source in selected if source.text.strip()]
        for item in result["items"]:
            item["values"]["inferred_session"] = session
            item["values"]["chunk_source_ids"] = usable_ids
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
    metrics = {
        "selected_messages": len(messages),
        "inferred_sessions": len(groups),
        "summary_chunks": len(chunks),
        "method": SESSION_RULES_VERSION,
    }
    if gap is not None:
        metrics["gap_minutes"] = gap
    if saved_range is not None:
        covered = {key for item in items for key in item["values"]["chunk_source_ids"]}
        metrics["range_covered"] = outcome == "complete" and covered == {
            str(source.id) for source in messages
        }
        if items and not metrics["range_covered"]:
            outcome = "partial"
            warnings.append(
                "Some messages in the requested range lack summary coverage."
            )
    return output(
        "Source-cited summaries of selected inferred sessions.",
        items=items,
        metrics=metrics,
        warnings=list(dict.fromkeys(warnings)),
        outcome=outcome,
    )
