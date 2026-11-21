"""Source-linked finding summaries shared by case and evidence readers."""

from collections import defaultdict

from django.core.paginator import Paginator
from django.db.models import F, Q
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Left
from django.http import Http404

from core.analysis.automatic import evidence_runs
from core.analysis.catalogue import (
    REPORT_OPERATIONS,
    finding_queryset,
    is_result_detail,
    raw_finding_item,
)
from core.analysis.errors import AnalysisError
from core.analysis.forms import EvidenceAnalysisForm
from core.analysis.navigation import finding_url
from core.analysis.operations import OPERATIONS
from core.analysis.presentation import (
    measurement_formatter,
    participant_names,
    participant_text,
)
from core.analysis.projection import is_ai_suggestion, is_timing_only
from core.analysis.ranking import rank_findings
from core.analysis.services import SourceResolutionCache, resolve_inputs
from core.models import AnalysisFinding, AnalysisResult, AnalysisRun, CaseAnalysisBatch
from image.analysis import MIN_OCR_WORD_SCORE
from phone.analysis import presentation as phone_presentation


def additional_operations(kind):
    return {
        key: spec
        for key, spec in OPERATIONS.items()
        if kind in spec.kinds and spec.additional_on_evidence
    }


def evidence_findings_context(case, kind, item, *, form=None, results_page=None):
    """Keep analysis outcomes independent from the evidence collection page."""
    runs = evidence_runs(case, kind, item.pk)
    batches = CaseAnalysisBatch.objects.filter(
        case=case, scope__contains={"type": "added", "kind": kind, "id": str(item.pk)}
    )
    preparing = batches.filter(status=CaseAnalysisBatch.Status.QUEUED).first()
    latest_batch = preparing or batches.first()
    page = Paginator(runs, 25).get_page(results_page)
    has_saved_results = _evidence_outcomes(page, case)
    highlights, highlights_limited = evidence_highlights(case, kind, item.pk)
    return {
        "analysis_form": form if form is not None else EvidenceAnalysisForm(),
        "analysis_page_obj": page,
        "analysis_outcomes_open": results_page is not None
        or (has_saved_results and not highlights),
        "highlights": highlights,
        "highlights_limited": highlights_limited,
        "evidence_analysis_batch": latest_batch,
        "analysis_pending": preparing is not None
        or runs.filter(status__in=("queued", "running")).exists(),
        "has_evidence_analysis": latest_batch is not None or bool(page.paginator.count),
        "additional_operations": additional_operations(kind).items(),
    }


def _evidence_outcomes(page, case):
    summaries = dict(
        AnalysisResult.objects.filter(case=case, run_id__in=[run.pk for run in page])
        .annotate(summary=Left(KeyTextTransform("summary", "data"), 241))
        .values_list("run_id", "summary")
    )
    cache = SourceResolutionCache(case, with_content=False)
    for run in page:
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
    if ai_suggestion:
        cited = [
            source for source in sources if source.id in finding.get("source_ids", [])
        ]
        names = participant_names(cited)
        include_sender_role = run.operation != "session_summaries"
        label = participant_text(label, names, include_sender_role=include_sender_role)
        text = participant_text(text, names, include_sender_role=include_sender_role)
    excerpt = " ".join(text[:110].split())
    if len(text) > 110:
        excerpt += "…"
    score = finding.get("values", {}).get("Minimum word confidence (0–100)")
    return {
        "number": index + 1,
        "review_reason": review_reason,
        "index": index,
        "label": label,
        "text": text if ai_suggestion else finding.get("text", ""),
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


def evidence_highlights(case, kind, evidence_id):
    """Rank the complete source selection, then validate a bounded preview."""
    findings = finding_queryset(case, {"kind": kind, "evidence": evidence_id}).filter(
        weak=False
    )
    candidates = list(
        rank_findings(findings, case)
        .annotate(raw_item=raw_finding_item(), highlight_reason=F("candidate_reason"))
        .values(
            "result__run_id", "item_index", "raw_item", "highlight_reason", "repeat_key"
        )[:51]
    )
    limited = len(candidates) > 50
    candidates = candidates[:50]
    runs = AnalysisRun.objects.filter(case=case).in_bulk(
        {candidate["result__run_id"] for candidate in candidates}
    )
    cache = SourceResolutionCache(case, with_content=False)
    checked = {}
    shown = set()
    highlights = []
    for candidate in candidates:
        run_id = candidate["result__run_id"]
        run = runs.get(run_id)
        finding = candidate["raw_item"]
        repeat_key = candidate["repeat_key"]
        if run is None or not isinstance(finding, dict) or repeat_key in shown:
            continue
        if run_id not in checked:
            try:
                sources = resolve_inputs(run, case, cache=cache)
            except AnalysisError, Http404:
                sources = []
            checked[run_id] = (
                sources,
                {
                    str(source.id): source
                    for source in sources
                    if source.kind == kind and source.item.pk == evidence_id
                },
            )
        sources, selected_sources = checked[run_id]
        row = _highlight_row(
            finding,
            candidate["item_index"],
            run,
            case,
            sources,
            selected_sources,
            candidate["highlight_reason"],
        )
        if row:
            if repeat_key:
                shown.add(repeat_key)
            highlights.append(row)
            if len(highlights) == 12:
                break
    return highlights, limited and len(highlights) < 12


def case_highlights(runs, case):
    """Recent useful results, latest per operation/source, with current sources."""
    recent = list(
        runs.filter(result__case=case)
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
        highlights.extend(
            result_highlights(
                run.result,
                case,
                allowed=current,
                hidden_indices=hidden_indices.get(run.result.pk, ()),
            )
        )
        if len(highlights) >= 12:
            break
    return highlights[:12]


def result_highlights(result, case, *, allowed, hidden_indices):
    run = result.run
    operation = run.operation
    try:
        sources = resolve_inputs(run, case)
    except AnalysisError, Http404:
        return []
    selected_sources = {
        str(source.id): source
        for source in sources
        if not source.item.is_archived
        and (
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
        highlights.append(row)
        if len(highlights) == 3:
            break
    return highlights


def _highlight_row(finding, index, run, case, sources, selected_sources, reason=""):
    operation = run.operation
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
    values = finding.get("values", {})
    keys = measurement_keys.get(operation, ())
    fields = {
        key: values[key] for key in keys if values.get(key) not in (None, "", "Unknown")
    }
    if not (fields if keys else finding.get("text", "").strip() or reason):
        return None
    row = finding_row(finding, index, case, run, sources=sources, review_reason=reason)
    if row["weak_plate_region"] or row["low_ocr_score"]:
        return None
    if fields:
        row["summary_text"] = ""
        row["measurements"] = measurement_formatter(fields, case=case)(fields)
    if operation == "image_metadata":
        row["label"] = "Embedded metadata claims"
    if operation == "geocode_locations":
        row["label"] = "Geographic lookup candidate"
    row["anchors"] = [
        anchor
        for anchor in finding.get("anchors", [])
        if anchor.get("source_id") == str(source.id)
    ][:1]
    row["attachment_label"] = phone_presentation.addition_label(source, operation)
    row["partial"] = run.status == AnalysisRun.Status.PARTIAL
    row["source_title"] = source.item.title
    row["source_url"] = source.original_url
    return row
