from core.evidence.forms import EvidenceForm


class DocumentEvidenceForm(EvidenceForm):
    kind_label = "Document"
    original_fields = ("file",)
    type_fields = ("page_count",)
