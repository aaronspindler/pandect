"""Source-linked finding summaries shared by case and evidence readers."""

from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404

from core.analysis.automatic import evidence_runs
from core.analysis.errors import AnalysisError
from core.analysis.forms import EvidenceAnalysisForm
from core.analysis.navigation import finding_url
from core.analysis.operations import OPERATIONS, get_operation
from core.analysis.presentation import measurement_formatter
from core.analysis.projection import is_ai_suggestion
from core.analysis.services import resolve_inputs
from core.models import AnalysisResult, AnalysisReview, AnalysisRun
from image.analysis import MIN_OCR_WORD_SCORE
from phone.analysis import presentation as phone_presentation


def additional_operations(kind):
    return {
        key: spec
        for key, spec in OPERATIONS.items()
        if kind in spec.kinds and spec.additional_on_evidence
    }


def evidence_findings_context(case, kind, item, *, form=None, results_page=None):
    """Keep processing history independent from the evidence collection page."""
    runs = evidence_runs(case, kind, item.pk)
    local_runs = runs.filter(parameters__has_key="automatic_batch")
    local_analysis_pending = local_runs.filter(
        status__in=("queued", "running")
    ).exists()
    page = Paginator(runs, 25).get_page(results_page)
    for run in page:
        run.label = get_operation(run.operation).label
    return {
        "analysis_form": form if form is not None else EvidenceAnalysisForm(),
        "analysis_page_obj": page,
        "highlights": evidence_highlights(runs, case, kind, item.pk),
        "local_analysis_pending": local_analysis_pending,
        "analysis_pending": local_analysis_pending
        or runs.filter(status__in=("queued", "running")).exists(),
        "has_local_analysis": local_analysis_pending or local_runs.exists(),
        "additional_operations": additional_operations(kind).items(),
    }


def finding_row(finding, index, review, case, run, *, show_weak=False, history=None):
    label = finding["label"]
    values = finding.get("values", {})
    weak_plate = (
        run.operation == "plate_ocr" and values.get("Weak plate region") is True
    )
    value_first = run.operation in {
        "native_text",
        "entities",
        "ocr",
        "plate_ocr",
        "transcription",
        "copied_text",
    } or label in {"Selected message", "Recurring wording comparison"}
    text = (
        values.get("Weak region OCR output", "")
        if weak_plate
        else finding.get("text", "")
    ).strip()
    excerpt = " ".join(text[:110].split())
    if len(text) > 110:
        excerpt += "…"
    score = finding.get("values", {}).get("Minimum word confidence (0–100)")
    return {
        "number": index + 1,
        "index": index,
        "label": label,
        "summary_text": excerpt if excerpt != label else "",
        "value_first": value_first,
        "url": finding_url(
            case.pk, run.pk, index, show_weak=show_weak, history=history
        ),
        "latest_review": review,
        "ai_suggestion": is_ai_suggestion(run.operation, finding),
        "plate_score": values.get("Plate-likeness score (0–100)")
        if run.operation == "plate_ocr"
        else None,
        "weak_plate_region": weak_plate,
        "weak_ocr_text": text if weak_plate else "",
        "low_ocr_score": (
            run.operation in {"ocr", "plate_ocr"}
            and isinstance(score, int | float)
            and score < MIN_OCR_WORD_SCORE
        ),
    }


def evidence_highlights(runs, case, kind, evidence_id):
    """Select a few source-linked findings independently of the history page."""
    recent = (
        runs.filter(result__case=case)
        .filter(Q(previous_run__isnull=True) | Q(previous_run__case=case))
        .order_by("operation", "-created_at", "pk")
        .distinct("operation")
        .values_list("pk", "operation", "status")
    )
    priority = {
        "plate_ocr": 0,
        "image_metadata": 1,
        "entities": 2,
        "text_topics": 4,
    }
    highlights = []
    for run_id, _operation, status in sorted(
        recent, key=lambda row: priority.get(row[1], 3)
    ):
        if status not in {AnalysisRun.Status.COMPLETE, AnalysisRun.Status.PARTIAL}:
            continue
        result = (
            AnalysisResult.objects.filter(case=case, run_id=run_id, run__case=case)
            .select_related("run")
            .first()
        )
        if result is not None:
            highlights.extend(
                result_highlights(result, case, kind=kind, evidence_id=evidence_id)
            )
        if len(highlights) >= 12:
            break
    return highlights[:12]


def case_highlights(runs, case):
    """Recent useful results, latest per operation/source, with current sources."""
    recent = (
        runs.filter(result__case=case)
        .filter(Q(previous_run__isnull=True) | Q(previous_run__case=case))
        .exclude(
            operation__in=(
                "native_text",
                "ocr",
                "audio_metadata",
                "location_quality",
                "normalize_addresses",
                "text_topics",
            )
        )
        .select_related("result")
        .order_by("-created_at", "pk")[:40]
    )
    seen = set()
    highlights = []
    for run in recent:
        identities = {
            (
                run.operation,
                item["kind"],
                item["evidence_id"],
                item.get("attachment_id") or "",
            )
            for item in run.inputs
        }
        current = identities - seen
        seen.update(identities)
        if not current or run.status not in {
            AnalysisRun.Status.COMPLETE,
            AnalysisRun.Status.PARTIAL,
        }:
            continue
        highlights.extend(result_highlights(run.result, case, allowed=current))
        if len(highlights) >= 12:
            break
    return highlights[:12]


def result_highlights(result, case, *, kind=None, evidence_id=None, allowed=None):
    run = result.run
    operation = run.operation
    try:
        sources = resolve_inputs(run, case)
    except AnalysisError, Http404:
        return []
    selected_sources = {
        str(source.id): source
        for source in sources
        if (
            source.kind == kind and source.item.pk == evidence_id
            if kind is not None
            else not source.item.is_archived
            and (
                run.operation,
                source.kind,
                str(source.item.pk),
                str(source.attachment.pk) if source.attachment else "",
            )
            in allowed
        )
    }
    measurement_keys = {
        "image_metadata": ("Capture time candidate", "GPS candidate"),
        "audio_metadata": (
            "Decoded duration (seconds)",
            "Decoded sample rate (Hz)",
            "Decoded channel count",
            "Full-scale sample fraction",
        ),
        "geocode_locations": ("address", "latitude", "longitude"),
        "location_quality": ("Event time", "Position origin", "Address"),
    }
    highlights = []
    reviews = {
        review.item_index: review
        for review in result.reviews.filter(case=case)
        .only("item_index", "decision")
        .order_by("item_index", "-created_at", "pk")
        .distinct("item_index")
    }
    rejected = AnalysisReview.Decision.REJECTED
    if None in reviews and reviews[None].decision == rejected:
        return []

    findings = enumerate(result.data.get("items", []))
    if operation == "plate_ocr":
        findings = sorted(
            findings,
            key=lambda entry: (
                entry[1].get("values", {}).get("Minimum word confidence (0–100)") or 0
            ),
            reverse=True,
        )
    plate_readings = set()
    for index, finding in findings:
        review = reviews.get(index)
        source = next(
            (
                selected_sources[key]
                for key in finding.get("source_ids", [])
                if key in selected_sources
            ),
            None,
        )
        if source is None or (review and review.decision == rejected):
            continue
        values = finding.get("values", {})
        keys = measurement_keys.get(operation, ())
        fields = {
            key: values[key]
            for key in keys
            if values.get(key) not in (None, "", "Unknown")
        }
        if not (fields if keys else finding.get("text", "").strip()):
            continue
        row = finding_row(finding, index, review, case, run)
        if row["weak_plate_region"] or row["low_ocr_score"]:
            continue
        if operation == "plate_ocr":
            if row["summary_text"] in plate_readings:
                continue
            plate_readings.add(row["summary_text"])
        if fields:
            row["summary_text"] = ""
            row["measurements"] = measurement_formatter(fields, case=case)(fields)
        if operation == "image_metadata":
            row["label"] = "Embedded metadata claims"
        if operation == "geocode_locations":
            row["label"] = (
                "Reviewed geographic estimate"
                if review and review.decision == AnalysisReview.Decision.ACCEPTED
                else "Geographic lookup candidate"
            )
        if operation == "text_topics" and "occurrences" in values:
            row["summary_text"] = ""
            row["label"] = f"Keyword · {finding['label']}"
        row["anchors"] = [
            anchor
            for anchor in finding.get("anchors", [])
            if anchor.get("source_id") == str(source.id)
        ][:1]
        row["attachment_label"] = phone_presentation.addition_label(source, operation)
        row["partial"] = run.status == AnalysisRun.Status.PARTIAL
        row["source_title"] = source.item.title
        row["source_url"] = source.original_url
        highlights.append(row)
        if len(highlights) == 3:
            break
    return highlights
