"""Supported retained evidence types, shared by forms, queries and analysis."""

from .models import (
    AudioEvidence,
    DocumentEvidence,
    FaceTimeEvidence,
    ImageEvidence,
    LocationEvidence,
    PersonLocationEvidence,
    PhoneCallEvidence,
    TextEvidence,
    TextMessageEvidence,
    VehicleLocationEvidence,
)

KINDS = {
    "image": ImageEvidence,
    "audio": AudioEvidence,
    "document": DocumentEvidence,
    "text": TextEvidence,
    "location": LocationEvidence,
    "person-location": PersonLocationEvidence,
    "vehicle-location": VehicleLocationEvidence,
    "text-message": TextMessageEvidence,
    "phone-call": PhoneCallEvidence,
    "facetime": FaceTimeEvidence,
}
KIND_LABELS = {
    "image": "Image",
    "audio": "Audio",
    "document": "Document",
    "text": "Text",
    "location": "Location",
    "person-location": "Person location",
    "vehicle-location": "Vehicle location",
    "text-message": "Text message",
    "phone-call": "Phone call",
    "facetime": "FaceTime",
}
FILE_KINDS = ("image", "audio", "document")
CALL_KINDS = ("phone-call", "facetime")
COMMUNICATION_KINDS = ("text-message", *CALL_KINDS)
COMMUNICATION_FIELDS = ("sender", "recipient", "occurred_at")
LOCATION_KINDS = ("location", "person-location", "vehicle-location")
LOCATION_FIELDS = ("address", "latitude", "longitude", "observed_at")
