from core.evidence.forms import EvidenceForm


class TextEvidenceForm(EvidenceForm):
    kind_label = "Text"
    original_fields = ("body",)
    type_fields = ("language",)
