"""Domain-owned analysis assumptions and parameter validation."""

from django import forms

from core.analysis.form_base import BaseAnalysisForm


class ImageAnalysisForm(BaseAnalysisForm):
    mode = forms.ChoiceField(
        required=False,
        choices=(("printed", "Printed English"),),
        initial="printed",
        label="Text profile",
        help_text=(
            "Processed locally. Handwriting and mixed text are unsupported. "
            "Inspect extracted text against the source image."
        ),
    )

    operation_fields = {"ocr": ("mode",)}
    field_order = [
        "mode",
        "backend",
        "operation",
        "submission_id",
        "sources",
        "external_processing",
    ]

    def parameter_values(self):
        mode = self.cleaned_data.get("mode")
        return {"mode": mode} if mode not in (None, "") else {}
