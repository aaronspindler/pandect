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

    operation_fields = {"ocr": ("mode",), "plate_ocr": ()}
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

    def clean(self):
        data = super().clean()
        if self.operation_name == "plate_ocr" and len(data.get("sources", [])) != 1:
            self.add_error("sources", "Choose one original image for plate search.")
        return data
