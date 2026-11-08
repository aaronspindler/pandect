from django.contrib import admin

from core.evidence.admin import EvidenceAdmin
from text.forms import TextEvidenceForm
from text.models import TextEvidence


@admin.register(TextEvidence)
class TextEvidenceAdmin(EvidenceAdmin):
    kind = "text"
    original_fields = TextEvidenceForm.original_fields
    type_fields = TextEvidenceForm.type_fields
