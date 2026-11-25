from django import template
from django.core.exceptions import ValidationError
from django.http import Http404

from core.analysis.errors import AnalysisError, error_message
from core.analysis.forms import EvidenceAnalysisForm
from core.analysis.import_checks import imported_sources
from core.models import AnalysisRun, CaseAnalysisBatch

register = template.Library()


@register.inclusion_tag("core/analysis/import_checks.html", takes_context=True)
def import_checks(context, import_type, import_id):
    case = context["case"]
    scope = {"type": import_type, "id": str(import_id)}
    run = (
        AnalysisRun.objects.filter(
            case=case,
            pk=import_id,
            operation="imessage_import",
            method_version="2",
            parameters__phase="import",
        ).first()
        if import_type == "messages"
        else None
    )
    try:
        _, sources = imported_sources(case, scope)
        count = sources.count()
    except AnalysisError as error:
        if error.code == "import_membership_unknown":
            return {"case": case, "unavailable_message": error_message(error.code)}
        return {}
    except Http404, ValidationError:
        return {}
    batch = CaseAnalysisBatch.objects.filter(case=case, scope__contains=scope).first()
    return {
        "case": case,
        "can_edit": context.get("can_edit"),
        "import_type": import_type,
        "import_id": import_id,
        "eligible_count": count,
        "is_imessage": bool(run and batch and batch.scope.get("automatic")),
        "form": EvidenceAnalysisForm(),
        "batch": batch,
        "csrf_token": context.get("csrf_token"),
    }
