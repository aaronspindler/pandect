"""Transactional intake and confirmed background publication requests."""

from uuid import uuid5

from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404

from cases.permissions import get_case
from core.analysis.errors import AnalysisError
from core.analysis.services import resolve_inputs, start_run
from core.evidence.queries import get_evidence
from core.models import AnalysisRun
from core.storage import file_integrity, retain_uploaded_originals
from document.models import DocumentEvidence


def start_preview(actor, case_id, values):
    with retain_uploaded_originals() as originals:
        case = get_case(actor, case_id, edit=True, lock=True)
        original = values.get("file")
        if original:
            metadata = file_integrity(DocumentEvidence(file=original).file)
            title = (
                values.get("title")
                or f"Messages: {metadata['original_filename']}"[:255]
            )
            document = DocumentEvidence.objects.filter(
                submission_id=values["submission_id"]
            ).first()
            if document:
                if (
                    document.case_id != case.pk
                    or document.uploaded_by_id != actor.pk
                    or document.sha256 != metadata["sha256"]
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
        return start_run(
            actor,
            case.pk,
            "imessage_import",
            [{"kind": "document", "id": str(document.pk)}],
            parameters={"phase": "preview", "timezone": values["timezone"]},
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
    return sources[0].item


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
    document = preview_source(preview, case)
    if not preview.result.data.get("metrics", {}).get("ready_rows"):
        raise ValidationError("This review has no ready messages to import.")
    return start_run(
        actor,
        case.pk,
        "imessage_import",
        [{"kind": "document", "id": str(document.pk)}],
        parameters={
            "phase": "import",
            "timezone": preview.parameters["timezone"],
            "preview_run_id": str(preview.pk),
            "people": people,
        },
        submission_id=uuid5(preview.pk, "confirmed-import"),
        previous_run=preview,
    )
