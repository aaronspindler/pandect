from django.contrib import admin

from audio.forms import AudioEvidenceForm
from audio.models import AudioEvidence
from core.evidence.admin import EvidenceAdmin


@admin.register(AudioEvidence)
class AudioEvidenceAdmin(EvidenceAdmin):
    kind = "audio"
    original_fields = AudioEvidenceForm.original_fields
    type_fields = AudioEvidenceForm.type_fields
    file_evidence = True
