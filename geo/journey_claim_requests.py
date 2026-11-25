"""Resolve and queue a plan comparison against exact reviewed material."""

import hashlib
import json

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404

from cases.permissions import get_case
from core.analysis.errors import AnalysisError
from core.analysis.manual import start_selected_analysis
from core.analysis.services import snapshot_and_resolve_sources
from core.analysis.sources import selection, source_id
from core.evidence.types import KIND_LABELS
from geo.journey_claim_checks import check_source_integrity
from geo.journey_claim_inputs import expand_input

DERIVED_INPUT_PREFIXES = ("trip:", "finding:")


def comparison_selection(case, picked):
    """Separate explicitly chosen interpretations from their contributing inputs."""
    selections = {}
    options = {}
    refs, groups, observations = [], [], []
    for role, tokens in picked.items():
        for token in tokens:
            if token.startswith(DERIVED_INPUT_PREFIXES):
                option = expand_input(case, token, role)
                if option["finding_ref"] and option["finding_ref"] not in refs:
                    refs.append(option["finding_ref"])
                choices = option["selections"]
                if role == "claim":
                    options[token] = (
                        option["claim"]["source_id"],
                        option["claim"],
                        option["label"],
                    )
                else:
                    group = dict(option["group"])
                    for key in group["members"]:
                        if key not in observations:
                            observations.append(key)
                    group["members"] = [
                        observations.index(key) for key in group["members"]
                    ]
                    groups.append(group)
            else:
                choices = [selection(token)]
                key = source_id(choices[0])
                if role == "claim":
                    options[token] = (key, None, "")
                elif key not in observations:
                    observations.append(key)
            for choice in choices:
                selections[source_id(choice)] = choice
            if len(selections) > settings.PANDECT_ANALYSIS_MAX_SOURCES:
                raise ValidationError(
                    "Choose fewer sources or a smaller trip. All members must fit."
                )
    chosen = list(selections.values())
    try:
        snapshots, sources = snapshot_and_resolve_sources(case, chosen)
    except Http404:
        raise AnalysisError("source_unavailable") from None
    check_source_integrity(case, sources)
    by_id = {source.id: source for source in sources}
    claim_options = {}
    for token, (key, claim, label) in options.items():
        source = by_id[key]
        if not source.text.strip():
            raise ValidationError("Choose a retained passage for every claim.")
        claim_options[token] = {
            "source": source,
            "claim": claim,
            "label": label or _source_label(source),
            "quote": source.text[claim["start"] : claim["end"]] if claim else "",
        }
    parameters = {"observations": observations, "groups": groups, "finding_refs": refs}
    digest = hashlib.sha256(
        json.dumps([snapshots, options, parameters], sort_keys=True).encode()
    ).hexdigest()
    return chosen, sources, claim_options, parameters, digest


def _source_label(source):
    label = f"{KIND_LABELS[source.kind]} · {source.item.title}"
    if source.attachment:
        label += f" · Call addition {source.attachment.created_at:%Y-%m-%d %H:%M %Z}"
    if source.derived_run_id:
        label += f" · Extraction {source.derived_run_id[:8]}"
    return label


@transaction.atomic
def queue_comparison(actor, case_id, state):
    """Recheck the reviewed material under the case lock before queueing it."""
    case = get_case(actor, case_id, edit=True, lock=True)
    if not state.get("reviewed"):
        raise ValidationError(
            "Review its exact sources and interpretations before queuing."
        )
    selected, _, _, parameters, digest = comparison_selection(case, state["picked"])
    if digest != state["digest"]:
        raise ValidationError("The selection changed. Choose its exact sources again.")
    _, runs, created = start_selected_analysis(
        actor,
        case.pk,
        "journey_claims",
        selected,
        parameters | {"claims": state["claims"]},
        submission_id=state["submission_id"],
    )
    return runs[0], created
