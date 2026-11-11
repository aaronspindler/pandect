"""Source and page order for native document text."""

from core.analysis.presentation import group_by_source, prioritize_measurements


def present(operation, items, formatter):
    for item in items:
        prioritize_measurements(item, "Coverage", "Status", "Reason")
    return group_by_source(items)
