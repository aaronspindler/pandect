"""Source-linked finding summaries shared by case and evidence readers."""

from collections import defaultdict
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.db.models import F, Q
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Left
from django.http import Http404
from django.urls import reverse

from core.analysis.automatic import evidence_runs
from core.analysis.catalogue import (
    PER_PAGE,
    REPORT_OPERATIONS,
    finding_queryset,
    is_result_detail,
    raw_finding_item,
    resolved_findings,
)
from core.analysis.errors import AnalysisError
from core.analysis.forms import EvidenceAnalysisForm
from core.analysis.navigation import finding_url
from core.analysis.operations import OPERATIONS, PAIR_MESSAGE_OPERATIONS
from core.analysis.presentation import (
    measurement_formatter,
    participant_names,
    participant_text,
)
from core.analysis.projection import is_ai_suggestion, is_timing_only
from core.analysis.ranking import rank_findings
from core.analysis.source_resolution import (
    SourceResolutionCache,
    resolve_active_inputs,
    resolve_inputs,
)
from core.analysis.sources import source_id
from core.evidence.navigation import collection_params
from core.models import AnalysisFinding, AnalysisResult, AnalysisRun, CaseAnalysisBatch
from image.analysis import MIN_OCR_WORD_SCORE
from phone.analysis import presentation as phone_presentation


def additional_operations(kind, item):
    return {
        key: spec
        for key, spec in OPERATIONS.items()
        if kind in spec.kinds
        and spec.additional_on_evidence
        and not (
            kind == "text-message"
            and item.recipient_id is None
            and key in PAIR_MESSAGE_OPERATIONS
        )
    }


def evidence_findings_context(case, kind, item, params, *, form=None):
    """Keep analysis outcomes independent from the evidence collection page."""
    runs = evidence_runs(case, kind, item.pk)
    batches = CaseAnalysisBatch.objects.filter(
        case=case, scope__contains={"type": "added", "kind": kind, "id": str(item.pk)}
    )
    preparing = batches.filter(status=CaseAnalysisBatch.Status.QUEUED).first()
    latest_batch = preparing or batches.first()
    page = Paginator(runs, 25).get_page(params.get("results_page"))
    scope = {"kind": kind, "evidence": str(item.pk)}
    candidates = finding_queryset(case, scope | {"weak": "1"})
    ranked = (
        rank_findings(candidates, case)
        .annotate(raw_item=raw_finding_item(), run_id=F("result__run_id"))
        .only("id", "result_id", "item_index", "weak")
    )
    findings = Paginator(ranked, PER_PAGE).get_page(params.get("findings_page"))
    selection = dict(collection_params(params).items()) | scope
    if findings.number > 1:
        selection["findings_page"] = findings.number
    if "results_page" in params:
        selection["results_page"] = page.number
    findings.object_list = list(evidence_finding_rows(case, item, findings, selection))
    if kind == "image":
        from image.analysis.presentation import add_finding_previews

        add_finding_previews(case, findings.object_list)
    has_saved_results = _evidence_outcomes(page, case, selection)
    latest_run = (
        next(iter(page), None)
        if page.number == 1
        else runs.only("status", "batch_id", "created_at").first()
    )
    pending = (
        preparing is not None or runs.filter(status__in=("queued", "running")).exists()
    )
    activity = evidence_activity(case, pending, preparing, latest_batch, latest_run)
    return {
        "visible_structure_url": _evidence_layout_url(case, kind, item, runs, selection)
        if kind in {"image", "document"}
        else "",
        "analysis_form": form if form is not None else EvidenceAnalysisForm(),
        "analysis_page_obj": page,
        "analysis_outcomes_open": "results_page" in params
        or (has_saved_results and not findings.paginator.count),
        "findings_page": findings,
        "analysis_activity": activity,
        "analysis_pending": pending,
        "has_evidence_analysis": latest_batch is not None or bool(page.paginator.count),
        "additional_operations": additional_operations(kind, item).items(),
    }


def _evidence_layout_url(case, kind, item, runs, selection):
    selected = source_id({"kind": kind, "id": item.pk})
    run = runs.filter(
        operation="ocr",
        result__case=case,
        result__data__visible_structure__has_key=selected,
    ).first()
    if run is None:
        return ""
    try:
        resolve_inputs(
            run,
            case,
            require_current_review=False,
            cache=SourceResolutionCache(case, with_content=False, active_only=True),
        )
    except AnalysisError, Http404:
        return ""
    return reverse(
        "core:analysis:detail",
        args=[case.pk, run.pk],
        query={
            "layout": selected,
            "return_evidence": urlencode(selection | {"layout": "1"}),
        },
        fragment="visible-structure",
    )


def evidence_activity(case, pending, preparing, batch, latest_run):
    processing_url = reverse("core:analysis:processing", args=[case.pk])
    if pending:
        return {
            "text": "Preparing checks…" if preparing else "Checking…",
            "action": "Follow evidence analysis",
            "url": (
                f"{processing_url}?batch={preparing.pk}#processing-history"
                if preparing
                else f"{processing_url}#in-progress-heading"
            ),
        }
    # Preparation can stop after it has already queued some checks. Keep that
    # batch's failure visible until a newer independent check or batch replaces it.
    if batch and (
        not latest_run
        or batch.pk == latest_run.batch_id
        or batch.created_at >= latest_run.created_at
    ):
        if batch.status == "failed":
            return {
                "text": "Analysis preparation stopped.",
                "action": "Review stopped analysis",
                "url": f"{processing_url}?batch={batch.pk}#processing-history",
            }
        if latest_run and latest_run.created_at < batch.created_at:
            return None
    if latest_run and latest_run.status == "failed":
        return {
            "text": "The latest check failed.",
            "action": "Review failed check",
            "url": reverse("core:analysis:detail", args=[case.pk, latest_run.pk]),
        }
    return None


def evidence_finding_rows(case, evidence, findings, selection):
    for finding, run, sources, warning in resolved_findings(case, findings):
        navigation = {"return_evidence": urlencode(selection | {"row": finding.pk})}
        raw = finding.raw_item
        if (
            not sources
            or not isinstance(raw, dict)
            or set(raw.get("source_ids", [])) != {str(source.id) for source in sources}
        ):
            yield {
                "id": finding.pk,
                "unavailable": True,
                "label": "Finding unavailable",
                "url": finding_url(
                    case.pk, run.pk, finding.item_index, history=navigation
                ),
            }
            continue
        selected = next(source for source in sources if source.item == evidence)
        row = source_reading(
            raw,
            finding.item_index,
            run,
            case,
            sources,
            selected,
            reason=finding.review_reason,
            history=navigation,
            show_weak=finding.weak,
        )
        row.update(
            id=finding.pk,
            result_id=finding.result_id,
            item_index=finding.item_index,
            operation=run.operation,
            raw_item=raw,
            unavailable=False,
            warning=warning,
            other_sources=[
                {
                    "title": source.item.title,
                    "url": source.original_url,
                    "archived": source.item.is_archived,
                    "attachment_label": phone_presentation.addition_label(
                        source, run.operation
                    ),
                }
                for source in sources
                if source != selected
            ],
        )
        if finding.weak:
            row["weak_label"] = (
                "Timing only"
                if run.operation == "conversation_link"
                else "Weak plate region"
            )
        yield row


def _evidence_outcomes(page, case, selection):
    summaries = dict(
        AnalysisResult.objects.filter(case=case, run_id__in=[run.pk for run in page])
        .annotate(summary=Left(KeyTextTransform("summary", "data"), 241))
        .values_list("run_id", "summary")
    )
    cache = SourceResolutionCache(case, with_content=False, active_only=True)
    history_query = {
        "return_evidence": urlencode(selection | {"results_page": page.number})
    }
    for run in page:
        run.history_url = reverse(
            "core:analysis:detail", args=[case.pk, run.pk], query=history_query
        )
        operation = OPERATIONS.get(run.operation)
        run.label = operation.label if operation else "Retained analysis"
        run.execution = run.parameters.get("_execution")
        run.has_result = run.pk in summaries
        summary = summaries.get(run.pk)
        # A multi-source outcome belongs to the whole selection. Its canonical
        # page presents coverage and citations together.
        if not summary or len(run.inputs) != 1:
            continue
        try:
            resolve_inputs(run, case, require_current_review=False, cache=cache)
        except AnalysisError, Http404:
            run.result_unavailable = True
        else:
            run.outcome_summary = summary[:240] + ("…" if len(summary) > 240 else "")
    return bool(summaries)


def finding_row(
    finding,
    index,
    case,
    run,
    *,
    show_weak=False,
    history=None,
    sources=(),
    review_reason="",
):
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
    ai_suggestion = is_ai_suggestion(run.operation, finding)
    rewrite_participant_labels = ai_suggestion and run.operation != "claim_comparison"
    if rewrite_participant_labels:
        cited = [
            source for source in sources if source.id in finding.get("source_ids", [])
        ]
        names = participant_names(cited)
        label = participant_text(label, names, operation=run.operation)
        text = participant_text(text, names, operation=run.operation)
    excerpt = " ".join(text[:110].split())
    if len(text) > 110:
        excerpt += "…"
    score = finding.get("values", {}).get("Minimum word confidence (0–100)")
    return {
        "number": index + 1,
        "review_reason": review_reason,
        "index": index,
        "label": label,
        "text": text if rewrite_participant_labels else finding.get("text", ""),
        "summary_text": excerpt if excerpt != label else "",
        "short_value": excerpt
        if value_first and excerpt == text and excerpt != label
        else "",
        "value_first": value_first,
        "url": finding_url(
            case.pk, run.pk, index, show_weak=show_weak, history=history
        ),
        "ai_suggestion": ai_suggestion,
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


def case_highlights(case):
    """Recent saved findings from the latest published runs per operation/source."""
    recent = list(
        AnalysisRun.objects.filter(case=case, result__case=case)
        .filter(Q(previous_run__isnull=True) | Q(previous_run__case=case))
        .exclude(
            operation__in=(
                *REPORT_OPERATIONS,
                "native_text",
                "ocr",
                "audio_metadata",
                "location_quality",
                "normalize_addresses",
            )
        )
        .select_related("result")
        .order_by("-created_at", "pk")[:40]
    )
    hidden_indices = defaultdict(set)
    for result_id, item_index in AnalysisFinding.objects.filter(
        case=case,
        result_id__in=[run.result.pk for run in recent],
        hidden_from_catalogue=True,
    ).values_list("result_id", "item_index"):
        hidden_indices[result_id].add(item_index)
    seen = set()
    current_results = []
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
        current_results.append((run.result, current))
    # Run creation determines supersession; publication determines reading order.
    current_results.sort(key=lambda entry: entry[0].created_at, reverse=True)
    highlights = []
    for result, current in current_results:
        highlights.extend(
            result_highlights(
                result,
                case,
                allowed=current,
                hidden_indices=hidden_indices.get(result.pk, ()),
            )
        )
        if len(highlights) >= 3:
            break
    return highlights[:3]


def result_highlights(result, case, *, allowed, hidden_indices):
    run = result.run
    operation = run.operation
    try:
        sources = resolve_active_inputs(run, case)
    except AnalysisError, Http404:
        return []
    selected_sources = {
        str(source.id): source
        for source in sources
        if (
            run.operation,
            source.kind,
            str(source.item.pk),
            str(source.attachment.pk) if source.attachment else "",
        )
        in allowed
    }
    highlights = []
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
        if index in hidden_indices:
            continue
        row = _highlight_row(finding, index, run, case, sources, selected_sources)
        if row is None:
            continue
        if operation == "plate_ocr":
            if row["summary_text"] in plate_readings:
                continue
            plate_readings.add(row["summary_text"])
        highlights.append(
            row
            | {
                "record_type": "Finding",
                "title": row["summary_text"] or row["label"],
                "created_at": result.created_at,
            }
        )
        if len(highlights) == 3:
            break
    return highlights


def _highlight_row(finding, index, run, case, sources, selected_sources):
    operation = run.operation
    source = next(
        (
            selected_sources[key]
            for key in finding.get("source_ids", [])
            if key in selected_sources
        ),
        None,
    )
    if (
        source is None
        or is_result_detail(operation, finding)
        or is_timing_only(operation, finding)
    ):
        return None
    row = source_reading(finding, index, run, case, sources, source)
    if row["weak_plate_region"] or row["low_ocr_score"]:
        return None
    if operation in MEASUREMENT_KEYS:
        return row if row.get("measurements") else None
    return row if finding.get("text", "").strip() else None


MEASUREMENT_KEYS = {
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


def source_reading(
    finding,
    index,
    run,
    case,
    sources,
    source,
    *,
    reason="",
    history=None,
    show_weak=False,
):
    """Present a finding in the context of the original or call addition it cites."""
    operation = run.operation
    values = finding.get("values", {})
    keys = MEASUREMENT_KEYS.get(operation, ())
    if history and operation == "image_metadata":
        if finding["label"] == "Decoded image and metadata claims":
            keys = ("Decoded format", "Decoded width", "Decoded height", *keys)
        elif finding["label"] == "Interpreted XMP attributes":
            keys = values
    fields = {
        key: values[key] for key in keys if values.get(key) not in (None, "", "Unknown")
    }
    row = finding_row(
        finding,
        index,
        case,
        run,
        sources=sources,
        review_reason=reason,
        history=history,
        show_weak=show_weak,
    )
    if fields:
        row["summary_text"] = ""
        row["measurements"] = measurement_formatter(fields, case=case)(fields)
    if (
        operation == "image_metadata"
        and finding["label"] == "Decoded image and metadata claims"
    ):
        row["label"] = "Embedded metadata claims"
    if operation == "geocode_locations" and fields:
        row["label"] = "Geographic lookup candidate"
    row["anchors"] = [
        anchor
        for anchor in finding.get("anchors", [])
        if anchor.get("source_id") == str(source.id)
    ]
    row["attachment_label"] = phone_presentation.addition_label(source, operation)
    row["partial"] = run.status == AnalysisRun.Status.PARTIAL
    row["source_id"] = str(source.id)
    row["source_title"] = source.item.title
    row["source_url"] = source.original_url
    return row
