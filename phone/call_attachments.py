"""Retain immutable call additions and resolve resubmitted content."""

from cases.permissions import get_case
from core.analysis.automatic import start_added_evidence_analysis
from core.evidence.queries import get_evidence
from core.storage import file_integrity, retain_uploaded_originals
from phone.models import CallEvidenceAttachment


class ArchivedCall(Exception):
    pass


class CallAttachmentConflict(Exception):
    pass


def call_content_matches(attachment, data):
    transcript, original = data.get("transcript", ""), data.get("file")
    if attachment is None:
        return not (transcript or original)
    if attachment.transcript != transcript or bool(attachment.file) != bool(original):
        return False
    if not original:
        return True
    metadata = file_integrity(CallEvidenceAttachment(file=original).file)
    return (
        attachment.audio_sha256 == metadata["sha256"]
        and attachment.byte_size == metadata["byte_size"]
        and attachment.original_filename == metadata["original_filename"]
        and attachment.declared_content_type == metadata["declared_content_type"]
    )


def save_call_content(call, actor, data, submission_id):
    if not data.get("transcript") and not data.get("file"):
        return None
    return CallEvidenceAttachment.objects.create(
        case_id=call.case_id,
        uploaded_by=actor,
        submission_id=submission_id,
        **{
            "phone_call"
            if call.communication_kind == "phone-call"
            else "facetime": call
        },
        transcript=data.get("transcript", ""),
        file=data.get("file") or "",
    )


def append_call_content(actor, call, form):
    """Recheck the call under lock before retaining content and queuing analysis."""
    kind = call.communication_kind
    with retain_uploaded_originals() as originals:
        case = get_case(actor, call.case_id, edit=True, lock=True)
        call = get_evidence(case, kind, call.pk, lock=True)
        if call.is_archived:
            raise ArchivedCall
        if not form.is_valid():
            return None, False

        submission_id = form.cleaned_data["submission_id"]
        previous = CallEvidenceAttachment.objects.filter(
            case=case, uploaded_by=actor, submission_id=submission_id
        ).first()
        if previous is not None:
            if (
                previous.phone_call_id != (call.pk if kind == "phone-call" else None)
                or previous.facetime_id != (call.pk if kind == "facetime" else None)
                or not call_content_matches(previous, form.cleaned_data)
            ):
                raise CallAttachmentConflict
            return previous, False
        attachment = save_call_content(call, actor, form.cleaned_data, submission_id)
        if attachment.file:
            originals.append(attachment.file)
        start_added_evidence_analysis(
            actor, case.pk, kind, call.pk, attachment_id=attachment.pk
        )
        return attachment, True
