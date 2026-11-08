"""Compose deterministic text observations and optional model suggestions."""

from text.analysis import ai, deterministic


def text_topics(sources, parameters, *, infer=None):
    if not parameters.get("include_summary", True):
        return deterministic.keywords(sources, parameters)
    return ai.text_topics(sources, parameters, infer=infer)
