"""Versioned, inspectable case evidence for chat. Callers authorize the case first."""

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db.models import Case, CharField, Exists, F, Func, OuterRef, Value, When
from django.db.models.functions import Length, Substr
from django.http import Http404
from django.urls import reverse

from cases.permissions import get_case
from core.analysis.catalogue import finding_queryset, raw_finding_item
from core.analysis.errors import AnalysisError
from core.analysis.navigation import finding_url
from core.analysis.projection import is_ai_suggestion
from core.analysis.services import SourceResolutionCache, resolve_inputs
from core.case_search.retained import retained_passages
from core.evidence.queries import scoped_evidence
from core.evidence.types import (
    CALL_KINDS,
    COMMUNICATION_KINDS,
    FILE_KINDS,
    KIND_LABELS,
    KINDS,
)
from core.storage import private_file
from phone.conversations import conversation_url
from phone.models import CallEvidenceAttachment
from phone.presentation import call_party_labels
from text.analysis.synthesis import finding_text

MAX_WINDOW = 8192
EXTRACTIONS = ("native_text", "ocr", "plate_ocr", "transcription")
RELATIONS = {
    **dict.fromkeys(COMMUNICATION_KINDS, ("sender", "recipient")),
    **dict.fromkeys(CALL_KINDS, ("sender", "recipient", "import_row__batch")),
    "text-message": ("sender", "recipient", "communication_context__source_document"),
    "person-location": ("person",),
    "vehicle-location": ("vehicle", "import_batch__document"),
}


def surfaces():
    yield from (("original", kind) for kind in KINDS)
    yield from (("attachment", kind) for kind in CALL_KINDS)
    yield "passage", ""
    yield "finding", ""


def body_field(surface, kind):
    if surface == "original" and kind in ("text", "text-message"):
        return "body"
    return {"attachment": "transcript", "passage": "text"}.get(surface)


def rows(case, surface, kind, include_archived, through=None, *, retained=False):
    """Use current results for searches and pinned versions for saved citations."""
    if surface in ("original", "attachment"):
        originals = scoped_evidence(case, kind)
        if not include_archived:
            originals = originals.filter(is_archived=False)
        if through:
            originals = originals.filter(created_at__lte=through)
        if surface == "attachment":
            parent = "phone_call" if kind == "phone-call" else "facetime"
            qs = CallEvidenceAttachment.objects.filter(
                case=case, **{f"{parent}__in": originals}
            ).select_related(parent)
        else:
            qs = originals.defer("description", "source_description")
            if related := RELATIONS.get(kind):
                qs = qs.select_related(*related)
            if kind in CALL_KINDS:
                qs = qs.defer(
                    "import_row__batch__selection", "import_row__overlap_candidates"
                )
        date_field = "created_at"
    else:
        if surface == "passage":
            qs = retained_passages(case).filter(
                result__run__operation__in=EXTRACTIONS, text__regex=r"\S"
            )
        else:
            qs = (
                finding_queryset(case, {"history": "all"} if retained else {})
                .exclude(result__run__operation="evidence_chat")
                .exclude(
                    Exists(
                        retained_passages(case).filter(
                            result_id=OuterRef("result_id"),
                            item_index=OuterRef("item_index"),
                        )
                    )
                )
                .annotate(canonical_item=raw_finding_item())
            )
        qs = qs.select_related("result__run").defer(
            "result__data", "result__provenance"
        )
        date_field = "result__created_at"
    if through:
        qs = qs.filter(**{date_field + "__lte": through})
    return qs.order_by("pk")


def _body_digest(field):
    return Func(
        Func(Func(F(field), Value("UTF8"), function="convert_to"), function="sha256"),
        Value("hex"),
        function="encode",
        output_field=CharField(),
    )


def clipped(queryset, field, start=0, size=0, *, review=False, verified_body=None):
    """Read the digest, length, and bounded excerpt in the same SQL snapshot."""
    if not field:
        return queryset
    digest = _body_digest(field)
    length = Length(field)
    if review:
        # The evidence triggers advance this revision on any INSERT, UPDATE,
        # or DELETE of these body tables, including bulk and direct SQL edits.
        # Match the stamp in the same statement that reads the current excerpt.
        queryset = queryset.annotate(body_revision=F("case__activity__evidence"))
        if (
            isinstance(verified_body, dict)
            and type(verified_body.get("revision")) is int
            and isinstance(verified_body.get("digest"), str)
            and type(verified_body.get("length")) is int
        ):
            revision = verified_body["revision"]
            digest = Case(
                When(body_revision=revision, then=Value(verified_body["digest"])),
                default=digest,
                output_field=CharField(),
            )
            length = Case(
                When(body_revision=revision, then=Value(verified_body["length"])),
                default=length,
            )
    queryset = queryset.defer(field).annotate(body_length=length, body_digest=digest)
    if size:
        queryset = queryset.annotate(body_window=Substr(field, start + 1, size))
    return queryset


def dependency(kind, key):
    return {"kind": kind, "id": str(key)}


def original_keys(locator):
    """Original evidence identities represented by this excerpt, for provenance."""
    if locator["evidence_id"]:
        return {locator["record_key"]}
    if "cited_originals" in locator:
        return set(locator["cited_originals"])
    return {f"{dep['kind']}:{dep['id']}" for dep in locator["dependencies"]}


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def lines(fields, *, value_ranges=None):
    rendered = []
    offset = 0
    for name, value in fields:
        if value is None or value == "":
            continue
        prefix = f"{name}: "
        line = f"{prefix}{value}"
        if value_ranges is not None and name != "Type":
            value_ranges.append((offset + len(prefix), offset + len(line)))
        rendered.append(line)
        offset += len(line) + 1  # Newline separating the fields.
    return "\n".join(rendered)


def original_metadata(item, kind, *, value_ranges=None):
    fields = [
        ("Type", KIND_LABELS[kind]),
        ("Title", item.title),
        ("Source", item.source),
        ("Source URL", item.source_url),
        ("Collected", item.collected_at),
    ]
    deps = [dependency(kind, item.pk)]
    detail = ""
    if kind in COMMUNICATION_KINDS:
        sender, recipient = (
            call_party_labels(item, reported_only=True)
            if kind in CALL_KINDS
            else (str(item.sender), str(item.recipient))
        )
        fields.extend(
            (
                ("Reported sender", sender),
                ("Reported recipient", recipient),
                ("Occurred", item.occurred_at),
            )
        )
        if kind in CALL_KINDS:
            fields.append(("Reported duration", item.duration))
            if item.import_row_id:
                row = item.import_row
                detail = f"Export row {row.number}"
                fields.extend(
                    (
                        ("Export row", row.number),
                        ("Reported contact in export", row.raw.get("Contact")),
                        ("Reported service in export", row.raw.get("Service")),
                        ("Export timestamp", row.raw.get("Date")),
                        ("Export timezone", row.batch.timezone),
                    )
                )
                deps.append(dependency("document", row.batch.document_id))
        else:
            context = getattr(item, "communication_context", None)
            if (
                context
                and context.case_id == item.case_id
                and context.source_document.case_id == item.case_id
                and context.source_document.sha256 == context.source_sha256
            ):
                detail = f"Export row {context.source_row}"
                fields.extend(
                    (
                        ("Export row", context.source_row),
                        ("Reported sender in export", context.raw_sender),
                        ("Reported recipient in export", context.raw_recipient),
                        ("Export timestamp", context.raw_timestamp),
                        ("Export timezone", context.timezone),
                        ("Message ID", context.native_message_id),
                        ("Thread ID", context.native_thread_id),
                        ("Reply to", context.reply_to_id),
                    )
                )
                deps.append(dependency("document", context.source_document_id))
    elif kind.endswith("location"):
        values = item.original_data()
        fields.extend(
            (name.replace("_", " ").capitalize(), value)
            for name, value in values.items()
            if name not in {"schema", "import", "raw_row", "vehicle_id", "person_id"}
        )
        if kind == "person-location":
            fields.append(("Reported person", str(item.person)))
        if kind == "vehicle-location":
            fields.append(("Reported vehicle", str(item.vehicle)))
            if item.import_batch_id:
                detail = f"Source row {item.source_row}"
                fields.extend(
                    (
                        ("Source row", item.source_row),
                        (
                            "Source row fields",
                            json.dumps(
                                item.raw_row, sort_keys=True, ensure_ascii=False
                            ),
                        ),
                    )
                )
                deps.append(dependency("document", item.import_batch.document_id))
    elif kind in FILE_KINDS:
        for field in (
            "original_filename",
            "byte_size",
            "declared_content_type",
            "page_count",
            "width",
            "height",
            "duration",
            "sample_rate",
            "channel_count",
        ):
            if hasattr(item, field):
                fields.append(
                    (field.replace("_", " ").capitalize(), getattr(item, field))
                )
    return lines(fields, value_ranges=value_ranges), detail, deps


def available_file(case, item):
    if not getattr(item, "file", None):
        return True
    try:
        file = private_file(item, case.pk)
        return file.storage.exists(file.name)
    except OSError:
        return False


@dataclass
class Material:
    locator: dict
    url: str
    text: str
    literal_ranges: tuple[tuple[int, int], ...] = ()


def _make_material(
    surface,
    kind,
    evidence_id,
    item,
    deps,
    label,
    url,
    *,
    version,
    suffix="",
    gap="",
    start,
    size,
    value_ranges=None,
):
    """Use the same citation text at discovery, reading and verification."""
    body_length = getattr(item, "body_length", 0)
    value_offset = body_length + int(bool(body_length and suffix))
    if body_length and suffix:
        suffix = "\n" + suffix
    version = digest(
        json.dumps(
            [version, getattr(item, "body_digest", ""), suffix, label],
            ensure_ascii=False,
        )
    )
    locator = {
        "key": f"{surface}:{kind}:{item.pk}",
        "surface": surface,
        "pk": str(item.pk),
        "record_key": f"{kind}:{evidence_id or item.pk}",
        "kind": kind,
        "evidence_id": str(evidence_id) if evidence_id else None,
        "dependencies": deps,
        "version": version,
        "length": body_length + len(suffix),
        "gap": gap,
        "label": label,
    }
    if getattr(item, "body_revision", None) is not None:
        locator["verified_body"] = {
            "revision": item.body_revision,
            "digest": item.body_digest,
            "length": body_length,
        }
    if surface in ("finding", "passage"):
        locator["result_id"] = str(item.result_id)
    if surface == "original":
        locator["metadata_start"] = value_offset
    text = (
        getattr(item, "body_window", "")
        + suffix[max(0, start - body_length) : max(0, start + size - body_length)]
    )
    literal_ranges = []
    if value_ranges is not None:
        if body_length:
            literal_ranges.append((0, body_length))
        literal_ranges.extend(
            (value_offset + first, value_offset + last) for first, last in value_ranges
        )
    return Material(locator, url, text, tuple(literal_ranges))


def _original_material(
    case, surface, kind, item, start, size, include_archived, through, value_ranges
):
    gap = ""
    if surface == "original":
        original = item
        suffix, detail, deps = original_metadata(
            original, kind, value_ranges=value_ranges
        )
        if kind == "text" and item.body_digest != item.sha256:
            gap = "Original text failed its integrity check."
            if size:
                return None
        label = f"{KIND_LABELS[kind]} · {original.title}"
        if detail:
            label += " · " + detail
    else:
        original = item.call
        deps = [dependency(kind, original.pk)]
        suffix = lines(
            (("Recording", item.original_filename), ("Bytes", item.byte_size)),
            value_ranges=value_ranges,
        )
        heading = "Supplied transcript" if item.body_length else "Recording"
        label = f"{heading} · {original.title}"
    if not available_file(case, item):
        gap = "Original file is unavailable."
        if size:
            return None
    elif not size and (
        kind in FILE_KINDS
        or surface == "attachment"
        and item.file
        and not item.body_length
    ):
        attachment_id = item.pk if surface == "attachment" else None
        if not _has_usable_passage(
            case, kind, original, attachment_id, include_archived, through
        ):
            gap = (
                "Recording has no retained transcript available in this scope."
                if attachment_id
                else "File content has no retained extraction available in this scope."
            )
    url = reverse("core:evidence:detail", args=(case.pk, kind, original.pk))
    if surface == "attachment":
        url += f"#attachment-{item.pk}"
    elif kind == "text-message" and not original.is_archived:
        url = conversation_url(case, original)
    elif kind.endswith("location"):
        url += "#location-heading"
    elif kind in CALL_KINDS:
        url += "#communication-heading"
    return _make_material(
        surface,
        kind,
        original.pk,
        item,
        deps,
        label,
        url,
        version=[original.sha256, item.sha256],
        suffix=suffix,
        gap=gap,
        start=start,
        size=size,
        value_ranges=value_ranges,
    )


def _result_sources(case, item, include_archived):
    try:
        sources = resolve_inputs(
            item.result.run, case, cache=SourceResolutionCache(case, with_content=False)
        )
    except AnalysisError, Http404, ValidationError, TypeError, KeyError, ValueError:
        return []
    if not include_archived and any(s.item.is_archived for s in sources):
        return []
    return sources


def _passage_original(passage, sources):
    return next(
        (
            s.item
            for s in sources
            if s.kind == passage.kind
            and s.item.pk == passage.evidence_id
            and s.item.sha256 == passage.evidence_sha256
            and (s.attachment.pk if s.attachment else None) == passage.attachment_id
        ),
        None,
    )


def _has_usable_passage(case, kind, original, attachment_id, include_archived, through):
    # All pages of a retained result share its full source dependency check.
    versions = (
        rows(case, "passage", "", include_archived, through)
        .filter(
            kind=kind,
            evidence_id=original.pk,
            evidence_sha256=original.sha256,
            attachment_id=attachment_id,
        )
        .defer("text", "anchors")
        .order_by("result_id")
        .distinct("result_id")
    )
    return any(
        _passage_original(p, _result_sources(case, p, include_archived)) is not None
        for p in versions.iterator(chunk_size=25)
    )


def _passage_label(item, original):
    method = {
        "native_text": "Extracted text",
        "ocr": "OCR text",
        "plate_ocr": "Plate OCR candidate",
        "transcription": "Transcribed speech · AI suggestion",
    }[item.result.run.operation]
    label = f"{method} · {original.title}"
    for anchor in item.anchors:
        if "page" in anchor:
            return f"{label} · Page {anchor['page']}"
        if "start_seconds" in anchor and "end_seconds" in anchor:
            return (
                f"{label} · {anchor['start_seconds']}–{anchor['end_seconds']} seconds"
            )
    return label


def material(
    case,
    surface,
    kind,
    item,
    include_archived,
    start=0,
    size=0,
    *,
    sources=None,
    through=None,
    literal=False,
):
    value_ranges = [] if literal else None
    if surface in ("original", "attachment"):
        return _original_material(
            case,
            surface,
            kind,
            item,
            start,
            size,
            include_archived,
            through,
            value_ranges,
        )
    if sources is None:
        sources = _result_sources(case, item, include_archived)
    if not sources:
        return None
    run = item.result.run
    deps = [
        dependency(k, pk)
        for k, pk in sorted({(s.kind, str(s.item.pk)) for s in sources})
    ]
    suffix = ""
    if surface == "passage":
        original = _passage_original(item, sources)
        if original is None:
            return None
        kind, evidence_id = item.kind, item.evidence_id
        label = _passage_label(item, original)
    else:
        stored = item.canonical_item
        if not isinstance(stored, dict):
            return None
        cited_ids = set(stored.get("source_ids", []))
        cited = [s for s in sources if str(s.id) in cited_ids]
        if not cited or len(cited) != len(cited_ids):
            return None
        origins = {(s.kind, str(s.item.pk)) for s in cited}
        kind, evidence_id = (
            next(iter(origins)) if len(origins) == 1 else ("finding", None)
        )
        attribution = (
            "AI suggestion"
            if is_ai_suggestion(run.operation, stored)
            else "Derived finding"
        )
        label = f"{attribution} · {stored['label']}"
        suffix = finding_text(stored)
        if value_ranges is not None and (finding_body := stored.get("text")):
            # Keep the saved citation offsets while excluding generated labels
            # and the JSON used to present structured method output.
            first = len(stored["label"]) + 1
            value_ranges.append((first, first + len(finding_body)))
    found = _make_material(
        surface,
        kind,
        evidence_id,
        item,
        deps,
        label,
        finding_url(case.pk, run.pk, item.item_index),
        version=[str(item.result_id), item.item_index],
        gap="The retained analysis reports partial coverage."
        if run.status == "partial"
        else "",
        suffix=suffix,
        start=start,
        size=size,
        value_ranges=value_ranges,
    )
    if surface == "finding" and evidence_id is None:
        found.locator["cited_originals"] = sorted(f"{k}:{pk}" for k, pk in origins)
    return found


def discover(
    case,
    include_archived,
    *,
    through=None,
    original_kinds=None,
    review=False,
    filters=None,
):
    """Stream the evidence inventory without loading whole text bodies or recordings."""
    from .search_scope import scope_rows

    for surface, kind in surfaces():
        if original_kinds is not None and (
            surface != "original" or kind not in original_kinds
        ):
            continue
        queryset = scope_rows(
            rows(case, surface, kind, include_archived, through),
            case,
            surface,
            kind,
            filters,
            include_recordings=True,
        )
        sources, last_result = None, None
        if surface in ("passage", "finding"):
            queryset = queryset.order_by("result_id", "item_index", "pk")
        for item in clipped(
            queryset, body_field(surface, kind), review=review
        ).iterator(chunk_size=50):
            if getattr(item, "result_id", None) != last_result:
                last_result = item.result_id
                sources = _result_sources(case, item, include_archived)
            found = material(
                case,
                surface,
                kind,
                item,
                include_archived,
                sources=sources,
                through=through,
            )
            if found:
                yield found.locator


def read_window(
    case,
    locator,
    start=0,
    size=1600,
    *,
    include_archived=False,
    review=False,
    literal=False,
):
    if (
        not isinstance(locator, dict)
        or type(locator.get("length")) is not int
        or type(start) is not int
        or not 0 <= start <= locator["length"]
        or type(size) is not int
        or not 0 < size <= MAX_WINDOW
    ):
        return None
    surface = locator.get("surface")
    kind = locator.get("kind") if surface in ("original", "attachment") else ""
    if (surface, kind) not in surfaces():
        return None
    try:
        pk = UUID(locator["pk"])
    except ValueError, KeyError, AttributeError, TypeError:
        return None
    queryset = rows(case, surface, kind, include_archived, retained=True).filter(pk=pk)
    item = clipped(
        queryset,
        body_field(surface, kind),
        start,
        size,
        review=review,
        verified_body=locator.get("verified_body"),
    ).first()
    if item is None:
        return None
    found = material(
        case, surface, kind, item, include_archived, start, size, literal=literal
    )
    if found is None or any(
        found.locator[k] != locator.get(k)
        for k in (
            "version",
            "key",
            "record_key",
            "evidence_id",
            "kind",
            "length",
            "dependencies",
            "result_id",
            "metadata_start",
        )
        if k in found.locator
    ):
        return None
    result = {
        "text": found.text,
        "start": start,
        "url": found.url,
        "label": found.locator["label"],
    }
    if "verified_body" in found.locator:
        result["verified_body"] = found.locator["verified_body"]
    if literal:
        result["literal_ranges"] = found.literal_ranges
    return result


def cite(case, locator, start, quote, *, include_archived=False, read=None):
    if not isinstance(quote, str) or not quote.strip() or len(quote) > MAX_WINDOW:
        return None
    window = (read or read_window)(
        case,
        locator,
        start,
        len(quote),
        include_archived=include_archived,
    )
    if window is None or window["text"] != quote:
        return None
    end = start + len(quote)
    url = window["url"]
    if (
        locator["surface"] == "original"
        and locator["kind"] == "text"
        and end < locator["metadata_start"]
    ):
        url = reverse(
            "core:evidence:detail",
            args=(case.pk, "text", locator["pk"]),
            query={"passage": f"{start}-{end}"},
            fragment="cited-passage",
        )
    return {
        "quote": quote,
        "start": start,
        "end": end,
        "url": url,
        "label": window["label"],
    }


def rank(
    case,
    question,
    *,
    user,
    include_archived=False,
    limit=10,
    through=None,
    related=None,
    hints=(),
    filters=None,
    stamp_body=False,
    semantic_hints=None,
):
    from .corpus_search import ranked_windows

    return ranked_windows(
        get_case(user, case.pk),
        question,
        user,
        include_archived,
        limit,
        through,
        related=related,
        hints=hints,
        filters=filters,
        stamp_body=stamp_body,
        semantic_hints=semantic_hints,
    )
