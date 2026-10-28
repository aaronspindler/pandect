"""Resolved input records; queue messages contain IDs rather than these objects."""

import json
from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from django.urls import reverse

from .navigation import finding_url


def source_id(choice):
    """Stable virtual source keys let users review a selection before enqueueing."""
    identity = [
        choice["kind"],
        str(choice.get("id") or choice.get("evidence_id")),
        str(choice.get("attachment_id") or ""),
        str(choice.get("result_id") or ""),
        choice.get("geocode_candidate"),
    ]
    return str(uuid5(NAMESPACE_URL, json.dumps(identity)))


@dataclass(frozen=True)
class Source:
    id: str
    kind: str
    item: object
    attachment: object = None
    extracted_text: str | None = None
    derived_coordinates: tuple | None = None
    derived_result_id: str | None = None
    derived_run_id: str | None = None
    segments: tuple = ()

    @property
    def text(self):
        if self.extracted_text is not None:
            return self.extracted_text
        if self.attachment is not None:
            return self.attachment.transcript
        return getattr(self.item, "body", "")

    @property
    def coordinates(self):
        if getattr(self.item, "latitude", None) is not None:
            return float(self.item.latitude), float(self.item.longitude)
        return self.derived_coordinates

    @property
    def url(self):
        if self.derived_run_id:
            finding = self.segments[0]["finding_index"] if self.segments else 0
            return finding_url(self.item.case_id, self.derived_run_id, finding)
        return self.original_url

    @property
    def original_url(self):
        url = reverse(
            "evidence:detail",
            kwargs={
                "case_id": self.item.case_id,
                "kind": self.kind,
                "evidence_id": self.item.pk,
            },
        )
        return url + (f"#attachment-{self.attachment.pk}" if self.attachment else "")

    def anchor(self, **position):
        anchor = {"source_id": str(self.id), **position}
        if self.derived_result_id:
            anchor["derived_result_id"] = self.derived_result_id
            if "start" in position and "end" in position:
                anchor["extraction_ranges"] = [
                    {
                        "finding_index": segment["finding_index"],
                        "start": max(position["start"], segment["start"])
                        - segment["start"],
                        "end": min(position["end"], segment["end"]) - segment["start"],
                        "original_anchors": segment["anchors"],
                    }
                    for segment in self.segments
                    if segment["start"] < position["end"]
                    and segment["end"] > position["start"]
                ]
        return anchor


def output(summary, *, items=None, metrics=None, warnings=None, outcome="complete"):
    """Common rendering shape for concrete operations, not an execution framework."""
    return {
        "summary": summary,
        "items": items or [],
        "metrics": metrics or {},
        "warnings": warnings or [],
        "outcome": outcome,
    }
