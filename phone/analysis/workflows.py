"""Compose observed communication links and optional model suggestions."""

from phone.analysis import ai
from phone.analysis.deterministic import conversations


def cross_channel_links(sources, parameters, *, infer=None):
    if parameters.get("semantic_links", False):
        return ai.semantic_cross_channel_links(sources, parameters, infer=infer)
    result = conversations.cross_channel_links(sources, parameters)
    result["metrics"].update(
        semantic_link_candidates=0,
        unresolved_semantic_suggestions=0,
    )
    return result
