"""Explicit append-only recording for operator reference-gallery decisions."""

from pandect.identities.models import SubjectReference, SubjectReferenceDecision


def record_reference_decision(
    reference: SubjectReference,
    action: str,
    *,
    previous_subject=None,
    previous_notes: str = "",
) -> SubjectReferenceDecision:
    """Snapshot one selected, changed, or removed gallery assignment."""
    return SubjectReferenceDecision.objects.create(
        reference_id=reference.pk,
        observation=reference.observation,
        subject=reference.subject,
        previous_subject=previous_subject,
        action=action,
        subject_label=reference.subject.label,
        previous_subject_label=(
            previous_subject.label if previous_subject is not None else ""
        ),
        notes=reference.notes,
        previous_notes=previous_notes,
    )
