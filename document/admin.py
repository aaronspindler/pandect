from django.contrib import admin

from core.evidence.admin import EvidenceAdmin
from document.forms import DocumentEvidenceForm
from document.models import DocumentEvidence


@admin.register(DocumentEvidence)
class DocumentEvidenceAdmin(EvidenceAdmin):
    kind = "document"
    original_fields = DocumentEvidenceForm.original_fields
    type_fields = DocumentEvidenceForm.type_fields
    file_evidence = True
