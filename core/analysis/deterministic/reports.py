"""Full-selection exact-byte groups with bounded reads and paginated occurrences."""

from heapq import merge
from itertools import groupby, islice

from django.core.paginator import Paginator
from django.db.models import Count, F

from core.analysis.reporting import (
    BATCH_SIZE,
    _base_metrics,
    _instant,
    _period,
    _state,
    _url,
)
from core.evidence.queries import scoped_evidence
from core.evidence.types import CALL_KINDS, FILE_KINDS, KIND_LABELS
from phone.models import CallEvidenceAttachment


def _groups(queries):
    streams = [
        queryset.order_by()
        .values("report_checksum")
        .annotate(total=Count("pk"))
        .order_by("report_checksum")
        .values_list("report_checksum", "total")
        .iterator(chunk_size=BATCH_SIZE)
        for _, queryset in queries
    ]
    for checksum, rows in groupby(
        merge(*streams, key=lambda row: row[0]), key=lambda row: row[0]
    ):
        count = sum(row[1] for row in rows)
        if count > 1:
            yield checksum, count


def _occurrences(kind, queryset, checksum):
    for item in (
        queryset.filter(report_checksum=checksum)
        .order_by("created_at", "pk")
        .iterator(chunk_size=BATCH_SIZE)
    ):
        yield kind, item


def duplicate_report(case, state, start, end, page, page_size):
    queries = [
        (
            kind,
            _period(
                _state(scoped_evidence(case, kind), state), "created_at", start, end
            ).annotate(report_checksum=F("sha256")),
        )
        for kind in FILE_KINDS
    ]
    for kind in CALL_KINDS:
        parent = "phone_call" if kind == "phone-call" else "facetime"
        parents = _state(scoped_evidence(case, kind), state)
        attachments = CallEvidenceAttachment.objects.filter(
            case=case, **{f"{parent}__in": parents}
        ).exclude(audio_sha256="")
        queries.append(
            (
                kind,
                _period(attachments, "created_at", start, end)
                .select_related(parent)
                .annotate(report_checksum=F("audio_sha256")),
            )
        )
    total = sum(queryset.count() for _, queryset in queries)
    group_count = occurrences = 0
    for _, count in _groups(queries):
        group_count += 1
        occurrences += count
    page_obj = Paginator(range(occurrences), page_size).get_page(page)
    first = page_obj.start_index() - 1 if occurrences else 0
    last = page_obj.end_index()
    offset = 0
    items = []
    for number, (checksum, count) in enumerate(_groups(queries), start=1):
        if offset >= last:
            break
        if offset + count <= first:
            offset += count
            continue
        streams = [_occurrences(kind, queryset, checksum) for kind, queryset in queries]
        rows = merge(*streams, key=lambda row: (row[1].created_at, row[0], row[1].pk))
        for kind, item in islice(
            rows, max(0, first - offset), min(count, last - offset)
        ):
            call_attachment = kind in CALL_KINDS
            parent = item.call if call_attachment else item
            url = _url(case, kind, parent)
            if call_attachment:
                url += f"#attachment-{item.pk}"
            items.append(
                {
                    "label": f"Group {number} · {item.original_filename}",
                    "text": (
                        "Identical file-byte checksum; "
                        "retained sources remain separate."
                    ),
                    "url": url,
                    "values": {
                        "Group": number,
                        "Group occurrences": count,
                        "SHA-256 domain": "Original file bytes",
                        "SHA-256": checksum,
                        "Source type": f"{KIND_LABELS[kind]} audio addition"
                        if call_attachment
                        else KIND_LABELS[kind],
                        "Source ID": str(item.pk),
                        "Byte size": item.byte_size,
                        "Archived parent": parent.is_archived,
                        "Uploaded at (UTC)": _instant(item.created_at),
                    },
                }
            )
        offset += count
    metrics = _base_metrics(total, state, start, end, "File/addition upload time")
    metrics.update(
        {
            "Duplicate byte groups": group_count,
            "Occurrences in duplicate groups": occurrences,
        }
    )
    return {
        "summary": (
            f"{group_count} exact-byte duplicate groups among "
            f"{total} selected files and audio additions."
        ),
        "metrics": metrics,
        "items": items,
        "page_obj": page_obj,
        "warnings": [],
        "limitations": [
            "Equal byte checksums identify candidate file reuse, not independent "
            "corroboration or the same event. Communication-record and attachment "
            "manifest checksums are excluded. Different file bytes are not matched; "
            "no evidence is merged or deleted."
        ],
    }
