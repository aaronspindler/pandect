"""Source-backed display fields for the current Evidence workspace page."""

from collections import defaultdict

from django.db.models.functions import Substr

from core.evidence.queries import scoped_evidence
from core.evidence.source_clocks import date_only_message_dates
from core.evidence.types import COMMUNICATION_KINDS, FILE_KINDS, LOCATION_KINDS
from phone.presentation import message_excerpt

PERSON_FIELDS = ("first_name", "last_name", "email", "phone_number")
VEHICLE_FIELDS = ("make", "model", "year", "license_plate", "vin")


def _related_fields(relation, fields):
    return [f"{relation}__{field}" for field in fields]


def _page_records(case, kind, ids):
    records = scoped_evidence(case, kind).filter(pk__in=ids).order_by()
    if kind in COMMUNICATION_KINDS:
        fields = ["occurred_at", *_related_fields("sender", PERSON_FIELDS)]
        if kind == "text-message":
            return (
                records.select_related("sender")
                .only(*fields)
                .annotate(label_body=Substr("body", 1, 81))
            )
        return records.select_related("sender", "recipient").only(
            *fields, *_related_fields("recipient", PERSON_FIELDS)
        )
    if kind == "location":
        return records.only("observed_at", "address", "latitude", "longitude")
    if kind == "person-location":
        return records.select_related("person").only(
            "observed_at", *_related_fields("person", PERSON_FIELDS)
        )
    if kind == "vehicle-location":
        return records.select_related("vehicle").only(
            "observed_at", *_related_fields("vehicle", VEHICLE_FIELDS)
        )
    if kind == "audio":
        return records.only("collected_at", "duration", "original_filename")
    if kind in FILE_KINDS:
        return records.only("collected_at", "original_filename")
    return records.only("collected_at", "source")


def _details(kind, record):
    if kind == "text-message":
        return "Sender", str(record.sender)
    if kind in COMMUNICATION_KINDS:
        names = str(record.sender)
        if record.recipient_id != record.sender_id:
            names = f"{names}, {record.recipient}"
        return "Participants", names
    if kind == "person-location":
        return "Linked person", str(record.person)
    if kind == "vehicle-location":
        return "Vehicle", str(record.vehicle)
    if kind == "location":
        if address := record.address.strip():
            return "Address", address
        return "Coordinates", f"{record.latitude}, {record.longitude}"
    if kind == "audio" and record.duration is not None:
        return "Duration", str(record.duration)
    if kind in FILE_KINDS:
        return "Original file", record.original_filename
    return "Source", record.source.strip()


def _time(kind, record, message_dates):
    if kind == "text-message":
        if record.pk in message_dates:
            return "Message date", message_dates[record.pk]
        return "Message time", record.occurred_at
    if kind in COMMUNICATION_KINDS:
        return "Call time", record.occurred_at
    if kind in LOCATION_KINDS:
        return "Observed", record.observed_at
    return "Collected", record.collected_at


def attach_workspace_rows(case, rows):
    """Hydrate only visible rows through the same-case evidence boundary."""
    by_kind = defaultdict(dict)
    for row in rows:
        by_kind[row["kind"]][row["id"]] = row
    message_dates = date_only_message_dates(case, by_kind.get("text-message", ()))
    for kind, kind_rows in by_kind.items():
        for record in _page_records(case, kind, kind_rows):
            row = kind_rows[record.pk]
            details_label, details_value = _details(kind, record)
            time_label, time_value = _time(kind, record, message_dates)
            row.update(
                details_label=details_label,
                details_value=details_value,
                time_label=time_label,
                time_value=time_value,
                time_date_only=kind == "text-message" and record.pk in message_dates,
            )
            if kind == "text-message":
                row["title"] = message_excerpt(record.label_body)
