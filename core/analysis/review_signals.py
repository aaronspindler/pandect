"""Factual reasons for reviewing a retained finding, in priority order."""

import hashlib
import json
from datetime import datetime

# Keys are stored in the rebuildable finding projection. Keep v1 readable for backfill.
REVIEW_SIGNALS = {
    "used_estimate": "A saved analysis used this address estimate.",
    "native_reply": "The reply reference matches the other message’s recorded ID.",
    "location_difference": "The selected location claims have different positions.",
    "clock_order": "The recorded receipt time precedes the reported event time.",
    "matched_identifier": (
        "The same identifier appears in another original with different content."
    ),
    "contrary_citation": "The AI suggestion cites a possible counterexample.",
    "coverage_gap": "The gap between these observations exceeds the analysis limit.",
    "compared_passages": "Compare the two cited passages for reused wording.",
    "uncertain_speech": "Play the cited interval to check the uncertain transcription.",
    "plate_region": "Check the candidate plate reading against its image region.",
    "quoted_claim": "The suggested claim points to a specific passage you can check.",
}


def _number(value):
    return type(value) in (int, float)


def _recorded_time(raw):
    try:
        value = datetime.fromisoformat(raw)
    except TypeError, ValueError:
        return None
    return value if value.utcoffset() is not None else None


def classify_review_signal_v1(operation, item):
    """Promote known output shapes. Other findings receive normal priority."""
    values = item.get("values", {})
    source_ids = item.get("source_ids")
    if not isinstance(values, dict) or not isinstance(source_ids, list):
        return ""
    cited = {value for value in source_ids if isinstance(value, str) and value}
    if not cited:
        return ""
    anchors = [
        anchor
        for anchor in item.get("anchors") or ()
        if isinstance(anchor, dict)
        and isinstance(anchor.get("source_id"), str)
        and anchor["source_id"] in cited
    ]
    label = item.get("label", "")
    if operation == "conversation_link" and label == "Candidate exchange sequence":
        before, after = values.get("before"), values.get("after")
        if (
            isinstance(before, dict)
            and isinstance(after, dict)
            and isinstance(before.get("source_id"), str)
            and before["source_id"] in cited
            and isinstance(after.get("source_id"), str)
            and after["source_id"] in cited
            and before["source_id"] != after["source_id"]
            and isinstance(after.get("reply_to_id"), str)
            and after["reply_to_id"].strip()
            and after["reply_to_id"] == before.get("native_message_id")
            # Historical exchange results omit export scope on both sides.
            and before.get("source_document_id") == after.get("source_document_id")
        ):
            return "native_reply"
    if operation == "location_conflicts" and label == "Selected observation comparison":
        distance = values.get("Distance from first selected observation (m)")
        if values.get("Claimed event/place") and _number(distance) and distance > 0:
            return "location_difference"
    if operation == "location_quality" and label == "Observation quality":
        received = _recorded_time(values.get("Receipt time"))
        occurred = _recorded_time(values.get("Event time"))
        if received and occurred and received < occurred:
            return "clock_order"
    if operation == "question_synthesis" and values.get("section") == "contrary":
        if anchors or item.get("finding_citations"):
            return "contrary_citation"
    if label == "Coverage gap" and operation in {
        "candidate_visits",
        "candidate_trips",
        "movement_routines",
        "travel_discrepancies",
    }:
        gap, limit = (
            values.get("Gap seconds"),
            values.get("Maximum supported gap seconds"),
        )
        if len(cited) >= 2 and _number(gap) and _number(limit) and gap > limit >= 0:
            return "coverage_gap"
    if (
        operation == "transcription"
        and values.get("Model uncertainty") == "Review uncertain speech"
    ):
        if any(
            _number(a.get("start_seconds"))
            and _number(a.get("end_seconds"))
            and 0 <= a["start_seconds"] < a["end_seconds"]
            for a in anchors
        ):
            return "uncertain_speech"
    if (
        operation == "plate_ocr"
        and item.get("text")
        and not values.get("Weak plate region")
    ):
        if any(
            isinstance(a.get("region"), list) and len(a["region"]) == 4 for a in anchors
        ):
            return "plate_region"
    if operation in {"content_events", "copied_text"}:
        passages = {
            a["source_id"]
            for a in anchors
            if type(a.get("start")) is int
            and type(a.get("end")) is int
            and 0 <= a["start"] < a["end"]
            and isinstance(a.get("source_id"), str)
        }
        if (
            operation == "copied_text"
            and label == "Candidate reused wording"
            and len(passages) >= 2
        ):
            return "compared_passages"
        if operation == "content_events" and passages and "status" in values:
            return "quoted_claim"
    return ""


def review_fields_v1(operation, item, inputs, match_kind, match_key):
    """Group exact readings or repeated observations of the same material."""
    if match_kind and match_key:
        identity = [match_kind, match_key]
    else:
        source_ids = item.get("source_ids", [])
        lineage = {}
        for source_id in source_ids or ():
            if not isinstance(source_id, str):
                continue
            snapshot = inputs.get(source_id, {})
            checksum = snapshot.get("sha256")
            lineage[source_id] = (
                [snapshot.get("kind"), checksum] if checksum else source_id
            )
        # The same anchored text or measurements in a copy/rerun adds no new
        # review step. Distinct passages and differing measurements stay distinct.
        anchors = []
        for anchor in item.get("anchors") or ():
            if isinstance(anchor, dict) and isinstance(anchor.get("source_id"), str):
                source_id = anchor["source_id"]
                anchor = anchor | {"source_id": lineage.get(source_id, source_id)}
            anchors.append(anchor)
        identity = item | {"source_ids": list(lineage.values()), "anchors": anchors}
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return {
        "review_signal": classify_review_signal_v1(operation, item),
        "repeat_key": hashlib.sha256(encoded).hexdigest(),
    }
