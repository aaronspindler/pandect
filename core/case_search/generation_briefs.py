"""Prepare and resolve bounded, versioned source sections for document briefs."""

from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID

from django.db.models import Count, Exists, Func, IntegerField, OuterRef, Q, Sum, Value
from django.db.models.functions import Coalesce, Length

from core.analysis.errors import AnalysisError
from core.analysis.published import PASSAGE_OPERATIONS
from core.analysis.sources import Source
from core.case_search.semantic_model import CHUNK_BYTES
from core.case_search.semantic_sources import BODY_FIELDS, source_queryset
from core.evidence_chat import corpus
from core.models import ExtractedPassage
from core.storage import canonical_bytes

SECTION_CHARS = corpus.MAX_WINDOW
BRIEF_SOURCES = ("text", "attachment", "extraction")


def eligible_sections(case, source):
    field = BODY_FIELDS[source]
    qs = source_queryset(case, source)
    multi = Q(byte_length__gt=CHUNK_BYTES)
    if source == "extraction":
        # Include only pages from the same extraction of the same original.
        origin = Coalesce("attachment_id", Value(UUID(int=0)))
        qs = qs.alias(origin_attachment=origin)
        peers = (
            ExtractedPassage.objects.alias(origin_attachment=origin)
            .filter(
                case=case,
                result_id=OuterRef("result_id"),
                evidence_id=OuterRef("evidence_id"),
                kind=OuterRef("kind"),
                evidence_sha256=OuterRef("evidence_sha256"),
                origin_attachment=OuterRef("origin_attachment"),
            )
            .exclude(pk=OuterRef("pk"))
        )
        # Short pages of the same multi-page source still belong in its brief.
        multi |= Exists(peers)
        qs = qs.filter(result__run__operation__in=PASSAGE_OPERATIONS)
    return qs.alias(
        byte_length=Func(field, function="octet_length", output_field=IntegerField())
    ).filter(multi)


def brief_estimate(case):
    sources = sections = 0
    for kind in BRIEF_SOURCES:
        counts = eligible_sections(case, kind).aggregate(
            sources=Count("pk"),
            sections=Sum(
                (Length(BODY_FIELDS[kind]) + SECTION_CHARS - 1) / SECTION_CHARS,
                default=0,
            ),
        )
        sources += counts["sources"]
        sections += counts["sections"]
    return {"estimate": sources, "units": sections}


def section(case, kind, pk, start):
    """Read version, length and bounded content from the same original snapshot."""
    if (
        kind not in BRIEF_SOURCES
        or type(start) is not int
        or start < 0
        or start % SECTION_CHARS
    ):
        raise AnalysisError("source_changed")
    qs = eligible_sections(case, kind)
    surface = {"text": "original", "attachment": "attachment", "extraction": "passage"}[
        kind
    ]
    qs = (
        qs.select_related("result__run").defer("result__data", "result__provenance")
        if kind == "extraction"
        else qs
    )
    row = corpus.clipped(
        qs.filter(pk=pk), BODY_FIELDS[kind], start, SECTION_CHARS
    ).first()
    if row is None or start >= row.body_length:
        raise AnalysisError("source_changed")
    if kind == "attachment":
        evidence_kind = "phone-call" if row.phone_call_id else "facetime"
    else:
        evidence_kind = "text" if kind == "text" else row.kind
    # Check original dependencies, integrity and file availability.
    material = corpus.material(
        case,
        surface,
        evidence_kind,
        row,
        False,
        start,
        min(SECTION_CHARS, row.body_length - start),
    )
    if material is None:
        raise AnalysisError("source_changed")
    selection = {"kind": evidence_kind, "id": material.locator["evidence_id"]}
    if kind == "attachment":
        selection["attachment_id"] = str(row.pk)
    elif kind == "extraction":
        selection["result_id"] = str(row.result_id)
        if row.attachment_id:
            selection["attachment_id"] = str(row.attachment_id)
    # Briefs read the section body. A change to the chat display label or to
    # investigator-edited metadata does not change that input.
    version = sha256(
        canonical_bytes([kind, str(pk), start, selection, row.body_digest])
    ).hexdigest()
    return row, selection, version


@dataclass(frozen=True)
class BriefSource(Source):
    section_source: str = ""
    section_pk: str = ""
    start_offset: int = 0
    finding_index: int | None = None
    original_anchors: tuple = ()

    def anchor(self, *, start, end):
        first, last = self.start_offset + start, self.start_offset + end
        citation = {
            "source_id": self.id,
            "start": start,
            "end": end,
            "search_source": self.section_source,
            "search_source_id": self.section_pk,
            "search_start": first,
            "search_end": last,
            "quote": self.text[start:end],
        }
        if self.finding_index is not None:
            citation["derived_result_id"] = self.derived_result_id
            citation["extraction_ranges"] = [
                {
                    "finding_index": self.finding_index,
                    "start": first,
                    "end": last,
                    "original_anchors": list(self.original_anchors),
                }
            ]
        return citation


def resolve_brief(run, case):
    from core.analysis.source_resolution import SourceResolutionCache, resolve_snapshots

    if len(run.inputs) != 1:
        raise AnalysisError("source_changed")
    expected = run.parameters.get("_brief_section")
    if not isinstance(expected, dict) or set(expected) != {
        "type",
        "id",
        "start",
        "version",
    }:
        raise AnalysisError("source_changed")
    try:
        pk = UUID(expected["id"])
    except AttributeError, TypeError, ValueError:
        raise AnalysisError("source_changed") from None
    kind, offset = expected["type"], expected["start"]
    row, selection, version = section(case, kind, pk, offset)
    snapshot = run.inputs[0]
    if version != expected["version"] or (
        selection["kind"],
        selection["id"],
        selection.get("attachment_id"),
        selection.get("result_id"),
    ) != (
        snapshot["kind"],
        snapshot["evidence_id"],
        snapshot.get("attachment_id"),
        snapshot.get("result_id"),
    ):
        raise AnalysisError("source_changed")
    (original,) = resolve_snapshots(
        run.inputs,
        case,
        cache=SourceResolutionCache(case, with_content=False, active_only=True),
    )
    return [
        BriefSource(
            original.id,
            original.kind,
            original.item,
            original.attachment,
            extracted_text=row.body_window,
            derived_result_id=original.derived_result_id,
            derived_run_id=original.derived_run_id,
            segments=({"finding_index": row.item_index},)
            if kind == "extraction"
            else (),
            section_source=kind,
            section_pk=str(pk),
            start_offset=offset,
            finding_index=row.item_index if kind == "extraction" else None,
            original_anchors=tuple(row.anchors) if kind == "extraction" else (),
        )
    ]
