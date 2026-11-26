"""Coalesce equivalent single-document extractions under start_run's case lock."""

from allauth.account.models import EmailAddress
from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import ValidationError
from django.db.models import (
    Case,
    CharField,
    Exists,
    F,
    Func,
    JSONField,
    OuterRef,
    Q,
    Value,
    When,
)

from cases.models import CaseMembership
from core.models import AnalysisRun, ExtractionReuse

PUBLISHED = ("complete", "partial", "empty", "insufficient", "unsupported")
ACTIVE = ("queued", "running")
SCHEDULING = ("automatic_batch", "automatic_followup", "_reprocessing")


def identified_pdf(document):
    return (
        document.original_filename.lower().endswith(".pdf")
        or document.declared_content_type == "application/pdf"
    )


def equivalent_extractions(case_id, snapshots, version):
    # JSON containment uses the input index; exact equality also rules out other
    # source versions, ordering and multi-document extractions.
    return (
        AnalysisRun.objects.filter(
            case_id=case_id,
            operation="native_text",
            method_version=version,
            external_processing=False,
            inputs__contains=snapshots,
            inputs=snapshots,
        )
        .alias(
            output_options=Func(
                F("parameters"),
                Value(list(SCHEDULING), output_field=ArrayField(CharField())),
                function="",
                arg_joiner=" - ",
                output_field=JSONField(),
            )
        )
        .filter(output_options={})
    )


def find_extraction(case_id, snapshots, version, *, published):
    verified = EmailAddress.objects.filter(
        user_id=OuterRef("requested_by_id"), verified=True
    )
    editor = CaseMembership.objects.filter(
        case_id=case_id, user_id=OuterRef("requested_by_id"), role="editor"
    )
    available = (
        Q(status__in=ACTIVE, requested_by__is_active=True)
        & Exists(verified)
        & (Q(requested_by_id=F("case__owner_id")) | Exists(editor))
    )
    if published:
        available |= Q(status__in=PUBLISHED, result__case_id=case_id)
    return (
        equivalent_extractions(case_id, snapshots, version)
        .filter(available)
        .order_by(
            Case(When(status__in=PUBLISHED, then=0), default=1),
            "-created_at",
            "pk",
        )
        .first()
    )


def reuse_run(run, resolved, *, published):
    previous = (
        ExtractionReuse.objects.filter(
            case_id=run.case_id,
            requested_by_id=run.requested_by_id,
            submission_id=run.submission_id,
        )
        .select_related("run")
        .first()
    )
    if previous:
        if (
            previous.run.case_id != run.case_id
            or previous.run.inputs != run.inputs
            or previous.run.operation != run.operation
            or previous.parameters != run.parameters
            or previous.batch_id != run.batch_id
            or run.previous_run_id
        ):
            raise ValidationError(
                "This submission was already used for different analysis settings."
            )
        return previous.run
    if (
        run.operation != "native_text"
        or run.previous_run_id
        or len(run.inputs) != 1
        or not identified_pdf(resolved[0].item)
        or run.inputs[0].get("result_id")
        or run.inputs[0].get("attachment_id")
        or run.parameters.keys() - set(SCHEDULING)
    ):
        return None
    existing = find_extraction(
        run.case_id, run.inputs, run.method_version, published=published
    )
    if existing:
        ExtractionReuse.objects.create(
            case_id=run.case_id,
            run=existing,
            requested_by_id=run.requested_by_id,
            submission_id=run.submission_id,
            batch_id=run.batch_id,
            parameters=run.parameters,
        )
    return existing


def follow_reused_extractions(run, result):
    """Honor automatic follow-ups when another request queued the extraction first."""
    from core.analysis.automatic import follow_up_extraction

    requests = run.reuse_requests.filter(
        Q(batch__isnull=True) | Q(batch__case_id=run.case_id),
        case_id=run.case_id,
        parameters__automatic_followup=True,
    ).select_related("requested_by", "batch")
    for request in requests:
        follow_up_extraction(run, result, reuse=request)
