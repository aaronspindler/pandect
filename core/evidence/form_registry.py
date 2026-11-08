from django import forms

from audio.forms import AudioEvidenceForm
from core.evidence.forms import COMMON_FIELDS
from core.evidence.types import KINDS
from document.forms import DocumentEvidenceForm
from geo.forms import (
    LocationEvidenceForm,
    PersonLocationEvidenceForm,
    VehicleLocationEvidenceForm,
)
from image.forms import ImageEvidenceForm
from phone.forms import (
    FaceTimeEvidenceForm,
    PhoneCallEvidenceForm,
    TextMessageEvidenceForm,
)
from text.forms import TextEvidenceForm

FORM_CLASSES = {
    "image": ImageEvidenceForm,
    "audio": AudioEvidenceForm,
    "document": DocumentEvidenceForm,
    "text": TextEvidenceForm,
    "location": LocationEvidenceForm,
    "person-location": PersonLocationEvidenceForm,
    "vehicle-location": VehicleLocationEvidenceForm,
    "text-message": TextMessageEvidenceForm,
    "phone-call": PhoneCallEvidenceForm,
    "facetime": FaceTimeEvidenceForm,
}
ORIGINAL_FIELDS = {kind: form.original_fields for kind, form in FORM_CLASSES.items()}
TYPE_FIELDS = {kind: form.type_fields for kind, form in FORM_CLASSES.items()}


def evidence_form(kind, *, editing=False):
    base = FORM_CLASSES[kind]
    fields = COMMON_FIELDS + base.type_fields
    if not editing:
        fields += base.original_fields
    form = forms.modelform_factory(KINDS[kind], form=base, fields=fields)
    if editing:
        # Call content is append-only, never part of metadata editing.
        for name in ("transcript", "file"):
            if name not in fields:
                form.base_fields.pop(name, None)
    return form
