from django.contrib import admin

from core.evidence.admin import EvidenceAdmin
from image.forms import ImageEvidenceForm
from image.models import ImageEvidence


@admin.register(ImageEvidence)
class ImageEvidenceAdmin(EvidenceAdmin):
    kind = "image"
    original_fields = ImageEvidenceForm.original_fields
    type_fields = ImageEvidenceForm.type_fields
    file_evidence = True
