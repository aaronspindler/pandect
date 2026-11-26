"""Open, follow and review relationships against their case's current sources."""

import hashlib
import json
from uuid import UUID

from django.db.models import OuterRef, Q, Subquery
from django.utils import timezone

from cases.permissions import get_case
from core.analysis.errors import AnalysisError
from core.models import AnalysisReview, NarrativeRelationship


def _passage(case, surface, kind, lookup, start, quote, through):
    from core.evidence_chat.corpus import body_field, clipped, material, rows

    selection = rows(case, surface, kind, False, through=through).filter(**lookup)
    item = clipped(selection, body_field(surface, kind), start, len(quote)).first()
    if item is None:
        return None
    found = material(
        case, surface, kind, item, False, start, len(quote), through=through
    )
    if found and found.text == quote:
        return found
    return None


def publish_relationships(result, sources):
    sources = {str(source.id): source for source in sources}
    for index, item in enumerate(result.data["items"]):
        source = sources[item["source_ids"][0]]
        quote, anchor, values = item["text"], item["anchors"][0], item["values"]
        start = anchor["start"]
        if source.derived_result_id:
            ranges = anchor["extraction_ranges"]
            if len(ranges) != 1:
                raise AnalysisError("invalid_anchor")
            loc = ranges[0]
            surface, kind = "passage", ""
            lookup = {
                "result_id": source.derived_result_id,
                "item_index": loc["finding_index"],
                "evidence_id": source.item.pk,
                "attachment_id": getattr(source.attachment, "pk", None),
            }
            start = loc["start"]
        elif source.attachment:
            surface, kind, lookup = (
                "attachment",
                source.kind,
                {"pk": source.attachment.pk},
            )
        else:
            surface, kind, lookup = "original", source.kind, {"pk": source.item.pk}
        found = _passage(result.case, surface, kind, lookup, start, quote, None)
        if not found:
            raise AnalysisError("invalid_anchor")
        key = [
            found.locator["key"],
            start,
            quote,
            values["subject"].casefold(),
            values["predicate"],
            values["object"].casefold(),
            values["qualification"],
        ]
        digest = hashlib.sha256(
            json.dumps(key, ensure_ascii=False).encode()
        ).hexdigest()
        edge, created = NarrativeRelationship.objects.get_or_create(
            case=result.case,
            fingerprint=digest,
            defaults={
                "result": result,
                "item_index": index,
                "evidence_id": source.item.pk,
                "start": start,
                "quote": quote,
                "locator": found.locator,
                **{
                    k: values[k]
                    for k in ("subject", "predicate", "object", "qualification")
                },
            },
        )
        if not created and edge.locator["version"] != found.locator["version"]:
            # The passage and assertion still match. Retain the original review
            # while making the refreshed locator visible to later investigations.
            NarrativeRelationship.objects.filter(pk=edge.pk).update(
                locator=found.locator, available_at=timezone.now()
            )


def reviewed_edges(case):
    latest = AnalysisReview.objects.filter(
        case=case, result_id=OuterRef("result_id"), item_index=OuterRef("item_index")
    ).order_by("-created_at", "-id")
    return NarrativeRelationship.objects.filter(case=case, result__case=case).annotate(
        decision=Subquery(latest.values("decision")[:1])
    )


def open_edge(case, edge, through):
    locator = edge.locator
    surface = locator["surface"]
    kind = "" if surface == "passage" else locator["kind"]
    found = _passage(
        case, surface, kind, {"pk": locator["pk"]}, edge.start, edge.quote, through
    )
    if found and found.locator["version"] == locator["version"]:
        return found
    return None


def follow_leads(case, *, through, actor, source_id=None, term="", limit=10):
    case = get_case(actor, case.pk)
    if not case.external_ai_enabled or not (source_id or term):
        return []
    through = through or timezone.now()
    edges = (
        reviewed_edges(case)
        .filter(available_at__lte=through)
        .filter(Q(decision__isnull=True) | ~Q(decision="rejected"))
    )
    if source_id:
        try:
            edges = edges.filter(evidence_id=UUID(str(source_id)))
        except TypeError, ValueError:
            return []
    if term:
        # Traverse a named graph endpoint. Free-text passage discovery uses AI Search.
        name = str(term).strip()[:160]
        edges = edges.filter(Q(subject__iexact=name) | Q(object__iexact=name))
    limit = max(1, min(int(limit), 25))
    found = []
    for edge in edges.order_by("-available_at", "pk")[: limit * 3]:
        material = open_edge(case, edge, through)
        if material is None:
            continue
        found.append(
            {
                "subject": edge.subject,
                "predicate": edge.predicate,
                "object": edge.object,
                "qualification": edge.qualification,
                "status": edge.decision or "candidate",
                "locator": material.locator,
                "quote": edge.quote,
                "start": edge.start,
                "end": edge.start + len(edge.quote),
                "result_id": str(edge.result_id),
                "url": material.url,
            }
        )
        if len(found) >= limit:
            break
    return found
