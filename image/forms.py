from core.evidence.forms import EvidenceForm


class ImageEvidenceForm(EvidenceForm):
    kind_label = "Image"
    original_fields = ("file",)
    type_fields = ("width", "height")
