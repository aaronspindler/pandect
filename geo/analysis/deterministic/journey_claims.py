"""Compare attributed plans and reports with individual location samples."""

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from django.core.exceptions import ValidationError

from core.analysis.sources import output
from core.evidence.types import CALL_KINDS, FILE_KINDS, LOCATION_KINDS
from geo.analysis.deterministic.locations import (
    _position_kind,
    _subject,
    _time,
    coordinate,
    distance_m,
    normalize_address,
    number,
)

MAX_COMPARISONS = 1000
SUPPORT = "Supporting observation"
CONTRARY = "Contrary evidence"
UNRESOLVED = "Unresolved"
STATUS = ("proposed", "cancelled", "corrected", "reported", "unresolved")
TIME_BASIS = {
    "explicit": "Explicit date, time and offset",
    "anchored_relative": "Relative date interpreted from a cited reference",
    "unresolved": "Time or timezone unresolved",
}


def _span_key(span):
    return span["source_id"], span["start"], span["end"]


def _span(span, source_map):
    if not isinstance(span, dict) or not isinstance(span.get("source_id"), str):
        raise ValidationError("Choose a passage from a retained text source.")
    source = source_map.get(span["source_id"])
    if (
        source is None
        or source.kind not in ("text", "text-message", *CALL_KINDS, *FILE_KINDS)
        or (source.kind in FILE_KINDS and not source.derived_result_id)
        or (source.kind in CALL_KINDS and not source.attachment)
    ):
        raise ValidationError(
            "Choose original text, a transcript or a retained extraction."
        )
    start, end = span.get("start"), span.get("end")
    if (
        type(start) is not int
        or type(end) is not int
        or not 0 <= start < end <= len(source.text)
        or end - start > 4000
        or not source.text[start:end].strip()
    ):
        raise ValidationError("Choose an exact passage of up to 4,000 characters.")
    anchor = source.anchor(start=start, end=end)
    if source.derived_result_id and not anchor.get("extraction_ranges"):
        raise ValidationError(
            "The selected passage needs its retained extraction anchors."
        )
    return anchor


def _window(claim):
    basis = claim.get("time_basis", "unresolved")
    if not isinstance(basis, str) or basis not in TIME_BASIS:
        raise ValidationError("Choose how the plan's time was interpreted.")
    start = claim.get("time_start")
    end = claim.get("time_end") or start
    if not start and not end and basis == "unresolved":
        return None
    try:
        first, last = datetime.fromisoformat(start), datetime.fromisoformat(end)
        if first.utcoffset() is None or last.utcoffset() is None or first > last:
            raise ValueError
    except TypeError, ValueError, OverflowError:
        raise ValidationError(
            "Enter an ordered time window with an explicit offset for each time."
        ) from None
    return first, last


@dataclass
class Interpretation:
    fields: dict
    source: object
    subjects: tuple
    anchors: list
    window: tuple | None
    position: tuple | None
    radius: float
    address: dict


def claimed_subjects(claim):
    """One reading can explicitly name several people or vehicles together."""
    subjects = claim.get("subjects", [])
    if not isinstance(subjects, list) or "subject" in claim:
        raise ValidationError("Choose the subjects of this interpretation together.")
    try:
        for subject in subjects:
            kind, value = subject.split(":")
            if kind not in ("person", "vehicle") or str(UUID(value)) != value:
                raise ValueError
        if len(set(subjects)) != len(subjects):
            raise ValueError
    except AttributeError, ValueError, TypeError:
        raise ValidationError(
            "Choose each claimed case person or vehicle once."
        ) from None
    return tuple(sorted(subjects))


def _interpret(claim, sources):
    anchor = _span(claim, sources)
    if claim.get("status") not in STATUS:
        raise ValidationError("Choose the status of the cited plan or report.")
    subjects = claimed_subjects(claim)
    anchors = [anchor]
    citations = claim.get("citations", [])
    if not isinstance(citations, list) or len(citations) > 10:
        raise ValidationError("Choose at most ten additional passages for a claim.")
    if claim.get("time_basis") == "anchored_relative" and not claim.get("reference"):
        raise ValidationError(
            "Cite the date or time reference used for the relative wording."
        )
    for span in [*citations, claim.get("reference"), claim.get("applies_to")]:
        if span is not None:
            extra = _span(span, sources)
            if extra not in anchors:
                anchors.append(extra)
    if type(claim.get("place_resolved", False)) is not bool:
        raise ValidationError("Indicate whether the claimed place can be resolved.")
    address = claim.get("address") or ""
    if not isinstance(address, str):
        raise ValidationError("Enter the claimed place as text.")
    lat, lon = claim.get("latitude"), claim.get("longitude")
    try:
        position = coordinate(lat, lon) if lat is not None or lon is not None else None
        raw_radius = claim.get("radius_m")
        radius = number(
            {"radius_m": 100 if raw_radius is None else raw_radius}, "radius_m"
        )
    except ValueError, OverflowError:
        raise ValidationError(
            "Enter valid coordinates and a radius between 1 and 500,000 metres."
        ) from None
    return Interpretation(
        claim,
        sources[claim["source_id"]],
        subjects,
        anchors,
        _window(claim),
        position,
        radius,
        normalize_address(address),
    )


def validate_comparison(sources, parameters, *, check_size=False):
    """Resolve selected roles and settings identically on submission and execution."""
    sources = list(sources)
    if not sources or len(sources) > 500 or len({s.item.case_id for s in sources}) != 1:
        raise ValidationError("Choose at most 500 sources from one case.")
    by_id = {str(s.id): s for s in sources}
    claims = parameters.get("claims")
    observations = parameters.get("observations")
    if (
        not isinstance(claims, list)
        or not claims
        or not isinstance(observations, list)
        or not observations
    ):
        raise ValidationError(
            "Choose a claim passage and at least one location observation."
        )
    if len(claims) * len(observations) > MAX_COMPARISONS:
        raise ValidationError(
            "Narrow the selection to at most 1,000 claim and observation pairs."
        )
    if any(
        not isinstance(key, str)
        or key not in by_id
        or by_id[key].kind not in LOCATION_KINDS
        for key in observations
    ):
        raise ValidationError("Choose observations from the selected location sources.")
    samples = [by_id[key] for key in observations]
    if len({(sample.kind, sample.item.pk) for sample in samples}) != len(samples):
        raise ValidationError("Choose one interpretation of each location observation.")
    interpretations = [_interpret(claim, by_id) for claim in claims]
    distinct = set()
    for plan in interpretations:
        normalized = dict(plan.fields, subjects=plan.subjects)
        distinct.add(json.dumps(normalized, sort_keys=True))
    if len(distinct) != len(interpretations):
        raise ValidationError("Add each passage interpretation only once.")
    claim_keys = {_span_key(claim) for claim in claims}
    for claim in claims:
        if target := claim.get("applies_to"):
            if (
                claim["status"] not in ("cancelled", "corrected")
                or _span_key(target) not in claim_keys
                or _span_key(target) == _span_key(claim)
            ):
                raise ValidationError(
                    "Link this change to another selected claim passage."
                )
    memberships = defaultdict(list)
    groups = parameters.get("groups", [])
    if not isinstance(groups, list):
        raise ValidationError("Choose retained candidate journeys or visits.")
    for index, group in enumerate(groups):
        members = group.get("members") if isinstance(group, dict) else None
        if (
            not isinstance(members, list)
            or not members
            or any(type(i) is not int or not 0 <= i < len(samples) for i in members)
            or len(set(members)) != len(members)
            or group.get("origin") not in ("trip", "finding")
        ):
            raise ValidationError(
                "The candidate needs its exact ordered observation membership."
            )
        for member in members:
            memberships[member].append(index)
    if check_size:
        _result(interpretations, samples, memberships, retain=False)
    return interpretations, samples, memberships


def _place_relation(plan, sample):
    """An unequal spelling alone cannot establish that two places differ."""
    if not plan.fields.get("place_resolved"):
        return None, "The claimed place remains unresolved."
    reported = normalize_address(getattr(sample.item, "address", ""))
    address_match = None
    if plan.address["street_number"] and reported["street_number"]:
        expected = plan.address["comparison_key"]
        actual = reported["comparison_key"]
        if actual == expected:
            address_match = True
        elif (
            "," in expected
            and plan.address["street_number"] != reported["street_number"]
            and expected.partition(" ")[2] == actual.partition(" ")[2]
        ):
            address_match = False
    if plan.position is not None and sample.coordinates is not None:
        distance = distance_m(plan.position, sample.coordinates)
        inside = distance <= plan.radius
        description = (
            f"The sample is {distance:,.1f} m from the interpreted point "
            f"(radius {plan.radius:g} m)."
        )
        if address_match is not None and address_match != inside:
            return None, description + " Address wording and coordinates disagree."
        if not inside and sample.item.latitude is None:
            return None, description + " The position comes from an address estimate."
        return inside, description
    if address_match is True:
        return True, "The specific address wording agrees."
    if address_match is False:
        return (
            False,
            "Street numbers differ on the same street in the same locality.",
        )
    return (
        None,
        "The selected place names or positions cannot resolve the location comparison.",
    )


def _association(plan, sample, changed):
    status = plan.fields["status"]
    if changed or status in ("cancelled", "corrected"):
        return (
            UNRESOLVED,
            "A cited cancellation or correction qualifies this plan. "
            "The sample leaves attendance unresolved.",
        )
    if status == "unresolved":
        return UNRESOLVED, "The passage's meaning remains unresolved."
    if _subject(sample) not in plan.subjects:
        return (
            UNRESOLVED,
            "The claim and observation do not explicitly identify the same subject.",
        )
    if plan.fields.get("time_basis", "unresolved") == "unresolved" or not plan.window:
        return UNRESOLVED, "The claim's time or timezone remains unresolved."
    observed = _time(sample)
    if not observed:
        return UNRESOLVED, "The sample lacks a recorded observation time."
    start, end = plan.window
    if not start <= observed <= end:
        return (
            UNRESOLVED,
            "The sample falls outside the interpreted window. "
            "It supplies no coverage inside the window.",
        )
    matches, reason = _place_relation(plan, sample)
    if matches is True:
        return (
            SUPPORT,
            reason
            + " The subject and observed time are compatible with this interpretation.",
        )
    if matches is False:
        if start == end:
            return (
                CONTRARY,
                reason
                + " The same subject was reported elsewhere at the claimed instant.",
            )
        return (
            UNRESOLVED,
            reason + " This sample leaves the rest of the time window unobserved.",
        )
    return UNRESOLVED, reason


def _finding(index, label, reason, anchors, values, **extra):
    return {
        "claim_index": index,
        "label": label,
        "text": reason,
        "report_only": label == UNRESOLVED,
        "source_ids": list(dict.fromkeys(anchor["source_id"] for anchor in anchors)),
        "anchors": anchors,
        "values": values,
        **extra,
    }


def _items(plans, samples, memberships):
    changed = defaultdict(list)
    readings = defaultdict(set)
    for plan in plans:
        fields = plan.fields
        target = fields.get("applies_to")
        if target:
            changed[_span_key(target)].append(plan.anchors[0])
        # Two prior findings can cite exactly the same reading of the same
        # original. Producer IDs and extra citations do not create a conflict.
        readings[_span_key(fields)].add(
            (
                fields["status"],
                plan.subjects,
                fields.get("place_resolved", False),
                plan.address["comparison_key"],
                plan.position,
                plan.radius if plan.position else None,
                fields.get("time_basis", "unresolved"),
                plan.window,
                _span_key(target) if target else None,
            )
        )

    for index, plan in enumerate(plans):
        claim = plan.fields
        key = _span_key(claim)
        anchors = list(plan.anchors)
        anchors.extend(anchor for anchor in changed[key] if anchor not in anchors)
        values = {
            "Interpretation": index + 1,
            "Claim status": claim["status"].capitalize(),
            "Claim passage": plan.source.text[claim["start"] : claim["end"]],
            ("Claimed subjects" if len(plan.subjects) > 1 else "Claimed subject"): (
                list(plan.subjects) or "Unresolved"
            ),
            "Claimed place": claim.get("address") or "Unspecified",
            "Time basis": TIME_BASIS[claim.get("time_basis", "unresolved")],
            "Interpretation source": "Investigator selection",
        }
        if plan.position:
            values["Interpreted position"] = list(plan.position)
            values["Comparison radius (m)"] = plan.radius
        if plan.window:
            values["Window start"], values["Window end"] = [
                bound.isoformat() for bound in plan.window
            ]
        if claim.get("result_id"):
            values["Claim source"] = "AI suggestion with cited source passages"
        if (sender := getattr(plan.source.item, "sender_id", None)) is not None:
            values["sender_id"] = str(sender)
        if claim.get("applies_to"):
            change = "cancellation" if claim["status"] == "cancelled" else "correction"
            yield _finding(
                index,
                CONTRARY if len(readings[key]) == 1 else UNRESOLVED,
                f"The linked passage is interpreted as a {change} of the claim.",
                anchors,
                values,
            )
        for sample_index, sample in enumerate(samples):
            label, reason = _association(plan, sample, changed[key])
            if len(plan.subjects) > 1 and label in (SUPPORT, CONTRARY):
                reason += " This sample concerns only its recorded subject."
            if len(readings[key]) > 1:
                label = UNRESOLVED
                reason = "This passage has competing interpretations. " + reason
            observed_at = _time(sample)
            details = {
                **values,
                "Observation subject": _subject(sample) or "Unlinked observation",
                "Observed at": observed_at.isoformat() if observed_at else "Unknown",
                "Recorded place": sample.item.address or "No reported address",
                "Position origin": _position_kind(sample),
            }
            if sample.coordinates is not None:
                details["Sample coordinates"] = list(sample.coordinates)
            yield _finding(
                index,
                label,
                reason,
                [*anchors, sample.anchor()],
                details,
                observation_index=sample_index,
                group_indices=memberships[sample_index],
            )


def _result(plans, samples, memberships, *, retain):
    # Exact escaped JSON size, including separators. Check amplified native
    # extraction anchors and quotations before materializing an oversized result.
    counts = Counter()
    items = []
    size = 0
    limit = 4 * 1024 * 1024
    too_large = "The comparison exceeds 4 MiB. Select fewer passages or observations."
    for item in _items(plans, samples, memberships):
        size += len(json.dumps(item)) + (2 if size else 0)
        if size > limit:
            raise ValidationError(too_large)
        counts[item["label"]] += 1
        if retain:
            items.append(item)
    outcome = "complete"
    if counts[UNRESOLVED]:
        outcome = "insufficient" if counts[UNRESOLVED] == counts.total() else "partial"
    result = output(
        "Compare the cited plans and reports with the selected location samples.",
        metrics={
            "Claim interpretations": len(plans),
            "Selected observations": len(samples),
            **counts,
        },
        warnings=[
            "A compatible sample leaves attendance and driver identity unverified.",
            "Only recorded samples contribute. Gaps and trip segments establish "
            "neither continuous presence nor absence.",
        ],
        outcome=outcome,
    )
    if size + len(json.dumps(result)) > limit:
        raise ValidationError(too_large)
    result["items"] = items
    return result


def compare_plans(sources, parameters):
    return _result(*validate_comparison(sources, parameters), retain=True)
