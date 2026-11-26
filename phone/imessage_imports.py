"""Transactional intake and confirmed background publication requests."""

from uuid import uuid5

from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404

from cases.permissions import get_case
from core.analysis.errors import AnalysisError
from core.analysis.services import start_run
from core.analysis.source_resolution import resolve_inputs
from core.evidence.queries import get_evidence
from core.models import AnalysisRun
from core.storage import file_integrity, retain_uploaded_originals, staged_file_upload
from document.models import DocumentEvidence
from phone.analysis.deterministic.imessage import (
    DATE_BOUND_FIELDS,
    import_file_limit,
    message_import_roles,
)


def start_preview(actor, case_id, values):
    original = values.get("file")
    if original:
        get_case(actor, case_id, edit=True)
        if original.size > import_file_limit():
            raise AnalysisError("imessage_import_limit")
    with (
        staged_file_upload(original, limit=import_file_limit()),
        retain_uploaded_originals() as originals,
    ):
        case = get_case(actor, case_id, edit=True, lock=True)
        if original:
            metadata = file_integrity(DocumentEvidence(file=original).file)
            if metadata["byte_size"] > import_file_limit():
                raise AnalysisError("imessage_import_limit")
            title = (
                values.get("title")
                or f"Messages: {metadata['original_filename']}"[:255]
            )
            document = DocumentEvidence.objects.filter(
                case=case, uploaded_by=actor, submission_id=values["submission_id"]
            ).first()
            if document:
                if (
                    document.sha256 != metadata["sha256"]
                    or document.title != title
                    or document.source != values.get("source", "")
                ):
                    raise ValidationError(
                        "This upload submission was already used for "
                        "different material. Start another import."
                    )
            else:
                document = DocumentEvidence(
                    case=case,
                    uploaded_by=actor,
                    title=title,
                    source=values.get("source", ""),
                    file=original,
                    submission_id=values["submission_id"],
                )
                document.full_clean()
                document.save()
                originals.append(document.file)
        else:
            document = get_evidence(case, "document", values["document"].pk, lock=True)
            if document.byte_size > import_file_limit():
                raise AnalysisError("imessage_import_limit")
        return start_run(
            actor,
            case.pk,
            "imessage_import",
            [{"kind": "document", "id": str(document.pk)}],
            parameters={
                "phase": "preview",
                "timezone": values["timezone"],
                **{
                    field: values[field].isoformat()
                    for field in DATE_BOUND_FIELDS
                    if values.get(field) is not None
                },
            },
            submission_id=uuid5(values["submission_id"], "imessage-preview"),
        )


def preview_source(preview, case):
    if (
        preview.operation != "imessage_import"
        or preview.parameters.get("phase") != "preview"
    ):
        raise Http404("Message import not found.")
    sources = resolve_inputs(preview, case)
    if len(sources) != 1 or sources[0].kind != "document":
        raise AnalysisError("source_unavailable")
    return sources[0]


@transaction.atomic
def confirm_import(actor, case_id, preview_id, people):
    case = get_case(actor, case_id, edit=True, lock=True)
    try:
        preview = (
            AnalysisRun.objects.select_for_update()
            .select_related("result")
            .get(
                pk=preview_id,
                case=case,
                operation="imessage_import",
                method_version="2",
                parameters__phase="preview",
                status__in=("complete", "partial"),
                result__case=case,
            )
        )
    except AnalysisRun.DoesNotExist:
        raise ValidationError(
            "Wait for the CSV review to finish before importing."
        ) from None
    document = preview_source(preview, case).item
    if not preview.result.data.get("metrics", {}).get("ready_rows"):
        raise ValidationError("This review has no ready messages to import.")
    summary = preview.result.data.get("imessage", {})
    if not isinstance(people, dict) or set(people) != set(
        message_import_roles(summary)
    ):
        raise ValidationError(
            "Choose a person for each participant shown in the review."
        )
    parameters = {
        "phase": "import",
        "timezone": preview.parameters["timezone"],
        **{
            field: preview.parameters[field]
            for field in DATE_BOUND_FIELDS
            if field in preview.parameters
        },
        "preview_run_id": str(preview.pk),
        "people": people,
    }
    submission_id = uuid5(preview.pk, "confirmed-import")
    # An old confirmation can be replayed after the form stops choosing a route.
    previous = AnalysisRun.objects.filter(
        case=case,
        operation="imessage_import",
        parameters__phase="import",
        parameters__preview_run_id=str(preview.pk),
        submission_id=submission_id,
    ).first()
    if previous is not None:
        if previous.requested_by_id != actor.pk:
            raise ValidationError("This review already has a confirmed import.")
        if "analysis" in previous.parameters:
            parameters["analysis"] = previous.parameters["analysis"]
    return start_run(
        actor,
        case.pk,
        "imessage_import",
        [{"kind": "document", "id": str(document.pk)}],
        parameters=parameters,
        submission_id=submission_id,
        previous_run=preview,
    )
