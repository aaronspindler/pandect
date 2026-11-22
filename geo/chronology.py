"""Read saved journey intervals alongside their cited location observations."""

from collections import defaultdict
from datetime import UTC
from itertools import pairwise

from django.db.models import F, Q, Window
from django.db.models.functions import RowNumber

from core.analysis.catalogue import (
    finding_queryset,
    raw_finding_item,
    resolved_findings,
)
from core.analysis.navigation import finding_url
from core.models import AnalysisFindingSource

JOURNEYS_PER_OBSERVATION = 3
SUBJECT_FIELDS = {"person-location": "person_id", "vehicle-location": "vehicle_id"}


def _interval(item, sources):
    if not isinstance(item, dict) or item.get("label") != "Candidate journey":
        return None
    if len(sources) < 2 or any(
        source.kind not in SUBJECT_FIELDS or source.item.observed_at is None
        for source in sources
    ):
        return None
    subjects = {
        (source.kind, getattr(source.item, SUBJECT_FIELDS[source.kind]))
        for source in sources
    }
    ordered = sorted(sources, key=lambda s: (s.item.observed_at, s.item.pk))
    if (
        len(subjects) != 1
        or len({source.item.pk for source in ordered}) != len(ordered)
        or item.get("source_ids") != [source.id for source in ordered]
        or item.get("anchors") != [source.anchor() for source in ordered]
    ):
        return None
    times = [source.item.observed_at for source in ordered]
    largest_gap = max(second - first for first, second in pairwise(times))
    values = item.get("values")
    if not isinstance(values, dict) or any(
        values.get(key) != value
        for key, value in {
            "First observed": times[0].astimezone(UTC).isoformat(),
            "Last observed": times[-1].astimezone(UTC).isoformat(),
            "Samples": len(times),
            "Largest sampling interval (seconds)": largest_gap.total_seconds(),
        }.items()
    ):
        return None
    return {
        "start_at": times[0],
        "end_at": times[-1],
        "largest_gap": largest_gap,
        "samples": len(times),
        "archived": any(source.item.is_archived for source in ordered),
    }


def attach_candidate_journeys(case, rows):
    """Keep pagination on originals; a saved interpretation can cite several rows."""
    observations = {
        (row["kind"], row["id"]): row
        for row in rows
        if row["kind"] in SUBJECT_FIELDS and row["event_label"] != "Collected"
    }
    if not observations:
        return
    findings = finding_queryset(
        case, {"method": "candidate_trips"}, labels=("Candidate journey",)
    )
    visible = Q(pk__in=[])
    for kind in SUBJECT_FIELDS:
        ids = [pk for source_kind, pk in observations if source_kind == kind]
        visible |= Q(kind=kind, evidence_id__in=ids)
    # Limit each observation separately so heavily analyzed originals cannot
    # consume the entire page's preview. The catalogue retains every alternative.
    references = (
        AnalysisFindingSource.objects.filter(visible, case=case, finding__in=findings)
        .annotate(
            position=Window(
                RowNumber(),
                partition_by=("kind", "evidence_id"),
                order_by=(
                    F("finding__result__run__created_at").desc(),
                    F("finding__result__run_id").desc(),
                    "finding__item_index",
                    "pk",
                ),
            )
        )
        .filter(position__lte=JOURNEYS_PER_OBSERVATION)
        .values_list("kind", "evidence_id", "finding_id")
    )
    targets = defaultdict(list)
    for kind, pk, finding_id in references:
        targets[finding_id].append((kind, pk))
    selected = (
        findings.filter(pk__in=targets)
        .only("id", "result_id", "item_index")
        .annotate(raw_item=raw_finding_item(), run_id=F("result__run_id"))
    )
    for finding, run, sources, warning in resolved_findings(case, selected):
        if interval := _interval(finding.raw_item, sources):
            interval.update(
                url=finding_url(case.pk, run.pk, finding.item_index),
                version=run.method_version,
                partial=run.status == "partial",
                warning=warning,
            )
            for key in targets[finding.pk]:
                observations[key].setdefault("journeys", []).append(interval)
