"""Report forms and explicit dispatch to domain analysis forms."""

from uuid import uuid4

from django import forms

from core.analysis.form_base import BaseAnalysisForm
from core.analysis.report_forms import BaseReportForm
from geo.analysis_forms import GeoAnalysisForm
from geo.report_forms import VehicleReportForm
from image.analysis_forms import ImageAnalysisForm
from phone.analysis_forms import PhoneAnalysisForm
from phone.report_forms import CallReportForm
from text.analysis_forms import TextAnalysisForm


def report_form(*args, case, report_name, **kwargs):
    """Build the ordinary Django form owned by the report's domain."""
    form_class = {
        "calls": CallReportForm,
        "vehicle-coverage": VehicleReportForm,
        "vehicle-speed": VehicleReportForm,
        "duplicates": BaseReportForm,
    }[report_name]
    return form_class(*args, case=case, **kwargs)


class EvidenceAnalysisForm(forms.Form):
    submission_id = forms.UUIDField(initial=uuid4, widget=forms.HiddenInput)


def analysis_form(*args, case, operation, query="", evidence=None, **kwargs):
    """Build the ordinary Django form owned by the selected operation's domain."""
    form_class = BaseAnalysisForm
    for candidate in (
        ImageAnalysisForm,
        GeoAnalysisForm,
        PhoneAnalysisForm,
        TextAnalysisForm,
    ):
        if operation in candidate.operation_fields:
            form_class = candidate
            break
    return form_class(
        *args, case=case, operation=operation, query=query, evidence=evidence, **kwargs
    )
