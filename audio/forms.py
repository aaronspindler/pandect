from core.evidence.forms import EvidenceForm


class AudioEvidenceForm(EvidenceForm):
    kind_label = "Audio"
    original_fields = ("file",)
    type_fields = ("duration", "sample_rate", "channel_count")
