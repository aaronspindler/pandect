"""Assemble bounded, local parent representations from canonical records."""

from uuid import UUID

from django.db.models import F, Func, IntegerField
from django.db.models.functions import Length

from core.case_search.parents import (
    EXCERPT,
    MAX_ANCHORS,
    active_sessions,
    anchor,
    summary_sources,
)
from core.case_search.retained import valid_sources
from core.case_search.semantic_model import CHUNK_BYTES
from core.case_search.semantic_sources import BODY_FIELDS, read_sources, source_queryset

SECTION = EXCERPT * 4
BATCH = 8


def origins(case, source):
    if source == "session":
        return active_sessions(case).filter(message_count__gte=2)
    if source == "summary":
        return summary_sources(case)
    rows = source_queryset(case, source)
    if source in ("text", "attachment"):
        return rows.alias(
            byte_length=Func(
                BODY_FIELDS[source],
                function="OCTET_LENGTH",
                output_field=IntegerField(),
            )
        ).filter(byte_length__gt=CHUNK_BYTES)
    return rows


def _sample(length, maximum=5):
    if length <= maximum:
        return range(length)
    return (n * (length - 1) // (maximum - 1) for n in range(maximum))


def _span(case, source, pk, offset=0, size=EXCERPT):
    piece = read_sources(case, source, {pk: offset}, size).get(pk)
    if piece and piece[0].strip():
        text, _ = piece
        return anchor(source, pk, offset, text), text
    return None


def _group(label, header, spans, *, coverage=None):
    spans = [s for s in spans if s]
    return {
        "label": label,
        "outline": "\n".join([header, *(text for _, text in spans)])[:8000],
        "anchors": [ref for ref, _ in spans],
        "coverage": coverage or {},
    }


def _composite_document(case, item):
    """Relate separately extracted PDF pages or transcript segments from one version."""
    siblings = (
        origins(case, "extraction")
        .filter(
            result_id=item.result_id,
            kind=item.kind,
            evidence_id=item.evidence_id,
            evidence_sha256=item.evidence_sha256,
            attachment_id=item.attachment_id,
        )
        .order_by("item_index", "pk")
        .values_list("pk", flat=True)
    )
    if siblings.first() != item.pk:
        return None
    count = siblings.count()
    if count < 2:
        return None
    spans = [_span(case, "extraction", siblings[n]) for n in _sample(count)]
    return _group("Document outline", "", spans) if any(spans) else None


def _section_context(source, item):
    if source != "extraction":
        return ""
    labels = []
    for point in item.anchors[:8]:
        if "page" in point:
            labels.append(f"Page {point['page']}")
        if "start_seconds" in point:
            labels.append(f"{point['start_seconds']} seconds")
    return " ".join(labels)


def document_rows(case, source, item, offset):
    length = item.content_length
    if not length or offset >= length:
        return [], None
    stop = min(length, offset + BATCH * SECTION)
    piece = read_sources(case, source, {item.pk: offset}, stop - offset).get(item.pk)
    if not piece:
        return [], None
    window, _ = piece
    rows = []
    header = _section_context(source, item)
    if not offset:
        if source == "extraction":
            composite = _composite_document(case, item)
            if composite:
                rows.append(("original", composite))
        if length > SECTION:
            spans = [
                _span(case, source, item.pk, n)
                for n in _sample(max(1, length - EXCERPT + 1))
            ]
            rows.append(("outline", _group("Document outline", header, spans)))
    for pos in range(offset, stop, SECTION):
        section = window[pos - offset : pos - offset + SECTION]
        spans = [
            (
                anchor(source, item.pk, pos + n, section[n : n + EXCERPT]),
                section[n : n + EXCERPT],
            )
            for n in range(0, len(section), EXCERPT)
            if section[n : n + EXCERPT].strip()
        ]
        rows.append((str(pos), _group("Document section", header, spans)))
    return rows, stop if stop < length else None


def session_rows(case, item, offset):
    members = item.members.filter(case=case).order_by("ordinal")
    batch = list(
        members.filter(ordinal__gt=offset).values_list("ordinal", "message_id")[
            : BATCH * BATCH
        ]
    )
    rows = []
    # Participant labels may change. The ranking query joins the current person rows.
    header = " ".join(t.isoformat() for t in (item.first_at, item.last_at) if t)
    sample = []
    if not offset and item.message_count > BATCH:
        ordinals = [n + 1 for n in _sample(item.message_count)]
        sample = list(
            members.filter(ordinal__in=ordinals).values_list("message_id", flat=True)
        )
    offsets = {pk: 0 for pk in [*sample, *(pk for _, pk in batch)]}
    spans = {
        pk: (anchor("message", pk, 0, body), body)
        for pk, (body, _) in read_sources(case, "message", offsets, EXCERPT).items()
        if body.strip()
    }
    if sample:
        rows.append(
            (
                "outline",
                _group("Conversation", header, [spans.get(pk) for pk in sample]),
            )
        )
    for i in range(0, len(batch), BATCH):
        group = batch[i : i + BATCH]
        rows.append(
            (
                str(group[0][0]),
                _group(
                    "Conversation",
                    header,
                    [spans.get(pk) for _, pk in group],
                    coverage={
                        "first": group[0][0],
                        "last": group[-1][0],
                        "total": item.message_count,
                    },
                ),
            )
        )
    return rows, batch[-1][0] if batch and batch[-1][0] < item.message_count else None


def summary_row(case, run, item):
    if not isinstance(item, dict):
        return None
    targets = {str(p["id"]): p for p in run.inputs}
    brief = run.operation == "text_topics"
    spans = []
    for citation in item.get("anchors", ())[:MAX_ANCHORS]:
        try:
            original = targets[str(citation["source_id"])]
            if brief:
                source = citation["search_source"]
                pk = UUID(citation["search_source_id"])
                offset = citation["search_start"]
                expected = citation["quote"]
                if (
                    source not in ("text", "extraction", "attachment")
                    or not isinstance(expected, str)
                    or not expected
                    or len(expected) > 4000
                ):
                    return None
                if source == "extraction":
                    if (
                        not origins(case, source)
                        .filter(
                            pk=pk,
                            result_id=original.get("result_id"),
                            evidence_id=original["evidence_id"],
                        )
                        .exists()
                    ):
                        return None
                elif str(pk) != str(
                    original.get("attachment_id") or original["evidence_id"]
                ):
                    return None
                length = len(expected)
            else:
                if original["kind"] != "text-message":
                    return None
                source, pk = "message", UUID(original["evidence_id"])
                offset = citation["start"]
                length = citation["end"] - offset
                expected = None
            if type(offset) is not int or offset < 0 or not 0 < length <= 4000:
                return None
            span = _span(case, source, pk, offset, length)
            if (
                not span
                or len(span[1]) != length
                or (expected is not None and span[1] != expected)
            ):
                return None
            text = span[1][:EXCERPT]
            spans.append((anchor(source, pk, offset, text), text))
        except KeyError, ValueError, TypeError, AttributeError:
            return None
    if not spans:
        return None
    scope = run.parameters.get("_saved_session_range", {})
    if scope and not active_sessions(case).filter(pk=scope.get("session")).exists():
        return None
    coverage = {
        "partial": run.status == "partial",
        **{k: scope[k] for k in ("first", "last") if k in scope},
    }
    return _group(
        "Document brief" if brief else "Conversation summary",
        str(item.get("text", "")),
        spans,
        coverage=coverage,
    )


def material(case, source, pk, offset=0):
    queryset = origins(case, source).filter(pk=pk)
    if source == "summary":
        # Read individual canonical statements in PostgreSQL. A large summary
        # result must not be deserialized wholesale on each worker delivery.
        slots = {
            f"statement_{n}": F(f"data__items__{offset + n}") for n in range(BATCH + 1)
        }
        result = (
            queryset.annotate(**slots)
            .select_related("run")
            .defer("data", "provenance")
            .first()
        )
        if not result or not valid_sources(result.run, case):
            return [], None
        rows = []
        for n in range(BATCH):
            item = getattr(result, f"statement_{n}")
            if row := summary_row(case, result.run, item):
                rows.append((str(offset + n), row))
        return rows, offset + BATCH if getattr(
            result, f"statement_{BATCH}"
        ) is not None else None
    if source == "session":
        item = queryset.first()
        return session_rows(case, item, offset) if item else ([], None)
    item = (
        queryset.defer(BODY_FIELDS[source])
        .annotate(content_length=Length(BODY_FIELDS[source]))
        .first()
    )
    return document_rows(case, source, item, offset) if item else ([], None)
