"""Cross-domain exact-byte duplicate reports over retained evidence."""

from collections import defaultdict

from core.analysis.reporting import (
    _base_metrics,
    _instant,
    _period,
    _selection,
    _state,
    _url,
)
from core.evidence.queries import scoped_evidence
from core.evidence.types import CALL_KINDS, FILE_KINDS, KIND_LABELS
from phone.models import CallEvidenceAttachment


def duplicate_report(case, state, start, end, limit):
    queries = [
        (
            kind,
            _period(
                _state(scoped_evidence(case, kind), state), "created_at", start, end
            ),
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
                _period(attachments, "created_at", start, end).select_related(parent),
            )
        )
    rows, available, warnings = _selection(queries, limit)
    groups = defaultdict(list)
    for kind, item in rows:
        checksum = item.sha256 if kind in FILE_KINDS else item.audio_sha256
        groups[checksum].append((kind, item))
    repeated = [
        (checksum, group)
        for checksum, group in sorted(groups.items())
        if len(group) > 1
    ]
    items = []
    for number, (checksum, group) in enumerate(repeated, start=1):
        for kind, item in group:
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
                        "Group occurrences": len(group),
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
    metrics = _base_metrics(
        rows, available, state, start, end, "File/addition upload time"
    )
    metrics.update(
        {
            "Duplicate byte groups": len(repeated),
            "Occurrences in duplicate groups": len(items),
        }
    )
    warnings.append(
        "Equal byte checksums identify candidate file reuse, not independent "
        "corroboration or the same event. Communication-record and attachment "
        "manifest checksums are excluded. Different file bytes are not matched; "
        "no evidence is merged or deleted."
    )
    return {
        "summary": (
            f"{len(repeated)} exact-byte duplicate groups among "
            f"{len(rows)} selected files and audio additions."
        ),
        "metrics": metrics,
        "items": items,
        "warnings": warnings,
    }
