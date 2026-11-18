"""Complete, case-scoped browsing of retained finding projections."""

from itertools import batched
from urllib.parse import urlencode

from django import forms
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import (
    Exists,
    F,
    Func,
    JSONField,
    OuterRef,
    Q,
    Subquery,
    TextField,
    Value,
)
from django.db.models.functions import Cast
from django.http import Http404

from core.analysis.errors import AnalysisError
from core.analysis.execution import requires_inference
from core.analysis.navigation import finding_url
from core.analysis.operations import OPERATIONS
from core.analysis.presentation import participant_names, participant_text
from core.analysis.projection import finding_values, is_timing_only
from core.analysis.ranking import rank_findings
from core.analysis.review_signals import classify_review_signal_v1
from core.evidence.queries import person_evidence, scoped_evidence
from core.evidence.search import content_excerpt
from core.evidence.types import KIND_LABELS, KINDS
from core.models import (
    AnalysisFinding,
    AnalysisFindingSource,
    AnalysisRun,
)
from people.models import Person

SUCCESSFUL = ("complete", "partial", "empty")
PER_PAGE = 25
TIMING_ONLY_FINDINGS = Q(result__run__operation="conversation_link", weak=True)
REPORT_OPERATIONS = (
    "comm_patterns",
    "session_summaries",
    "style_profiles",
    "text_topics",
)
REPORT_DETAIL_LABELS = {
    "conversation_link": (
        "Unresolved reference suggestion",
        "continued_topic",
        "correction",
    ),
    "style_change": (
        "Group A coverage",
        "Group B coverage",
        "Mean message words",
        "Mean sentence words",
        "Possible observed-style change interval",
        "Punctuation per 100 words",
        "Recurring wording comparison",
        "Uppercase letter ratio",
        "Vocabulary ratio",
    ),
    "transcription": (
        "Coarse transcript candidate",
        "Interval without transcribed text",
        "Unresolved recording interval",
    ),
}
ADJACENCY_BASIS = "temporal adjacency; lexical overlap is a separate weak content cue"


def is_result_detail(operation, item):
    """Keep routine report rows in the saved run."""
    label = item.get("label")
    return (
        operation in REPORT_OPERATIONS
        or item.get("report_only") is True
        or label in REPORT_DETAIL_LABELS.get(operation, ())
        or (
            operation == "conversation_link"
            and label == "Candidate exchange sequence"
            and item.get("values", {}).get("basis") == ADJACENCY_BASIS
            and classify_review_signal_v1(operation, item) != "native_reply"
        )
        or (
            operation == "transcription"
            and label == "Transcript segment candidate"
            and (
                not item.get("text", "").strip()
                or classify_review_signal_v1(operation, item) != "uncertain_speech"
            )
        )
    )


def result_detail_projections():
    # Match known legacy projections without loading canonical result JSON.
    details = (
        Q(result__run__operation__in=REPORT_OPERATIONS)
        | (
            Q(
                result__run__operation="conversation_link",
                label="Candidate exchange sequence",
                weak=False,
                search_text__contains=f"\nbasis: {ADJACENCY_BASIS}",
            )
            & ~Q(review_signal="native_reply")
        )
        | (
            Q(
                result__run__operation="transcription",
                label="Transcript segment candidate",
            )
            & ~Q(review_signal="uncertain_speech")
        )
    )
    for operation, labels in REPORT_DETAIL_LABELS.items():
        details |= Q(result__run__operation=operation, label__in=labels)
    return details


def raw_finding_item():
    """Fetch a single canonical item without loading the rest of the result."""
    return Func(
        F("result__data"),
        Value("items"),
        Cast(F("item_index"), TextField()),
        function="jsonb_extract_path",
        output_field=JSONField(),
    )


@transaction.atomic
def project_result(result):
    """Publish a rebuildable projection atomically alongside its canonical result."""
    inputs = {source["id"]: source for source in result.run.inputs}
    operation = result.run.operation
    items = (
        (index, item)
        for index, item in enumerate(result.data.get("items", []))
        if not (is_result_detail(operation, item) or is_timing_only(operation, item))
    )
    for chunk in batched(items, 500, strict=False):
        findings = [
            AnalysisFinding(
                case_id=result.case_id,
                result=result,
                item_index=index,
                **finding_values(operation, item, inputs=inputs),
            )
            for index, item in chunk
        ]
        AnalysisFinding.objects.bulk_create(findings, batch_size=500)
        references = (
            AnalysisFindingSource(
                case_id=result.case_id,
                finding=finding,
                source_id=key,
                kind=inputs[key]["kind"],
                evidence_id=inputs[key]["evidence_id"],
                attachment_id=inputs[key].get("attachment_id"),
                source_sha256=inputs[key]["sha256"],
            )
            for finding, (_, item) in zip(findings, chunk, strict=True)
            for key in dict.fromkeys(item.get("source_ids", []))
            if key in inputs
        )
        for source_chunk in batched(references, 500, strict=False):
            AnalysisFindingSource.objects.bulk_create(source_chunk, batch_size=500)


class CatalogueForm(forms.Form):
    q = forms.CharField(label="Search findings", required=False, max_length=200)
    method = forms.ChoiceField(label="Method", required=False)
    kind = forms.ChoiceField(
        label="Evidence type",
        required=False,
        choices=[("", "All evidence types"), *KIND_LABELS.items()],
    )
    evidence = forms.UUIDField(
        label="Specific evidence", required=False, widget=forms.HiddenInput
    )
    person = forms.ModelChoiceField(
        label="Person",
        queryset=Person.objects.none(),
        required=False,
        empty_label="All people",
    )
    history = forms.ChoiceField(
        label="Result history",
        required=False,
        choices=[
            ("", "Latest results"),
            ("all", "All saved results"),
        ],
    )
    weak = forms.ChoiceField(
        label="Timing-only findings",
        required=False,
        choices=[("", "Hide"), ("1", "Show")],
        widget=forms.HiddenInput,
    )

    def __init__(self, data, *, case):
        super().__init__(data)
        methods = set(
            AnalysisRun.objects.filter(case=case).values_list("operation", flat=True)
        )
        methods.update(OPERATIONS)
        self.fields["method"].choices = [
            ("", "All methods"),
            *sorted(
                (
                    (key, OPERATIONS[key].label if key in OPERATIONS else key)
                    for key in methods
                ),
                key=lambda choice: choice[1],
            ),
        ]
        self.fields["person"].queryset = Person.objects.filter(case=case)


def finding_queryset(case, filters, *, person=None, labels=None):
    """Return filtered items before pagination."""
    queryset = (
        AnalysisFinding.objects.filter(
            case=case,
            result__case=case,
            result__run__case=case,
            result__run__status__in=SUCCESSFUL,
            hidden_from_catalogue=False,
        )
        .filter(
            Q(result__run__previous_run__isnull=True)
            | Q(result__run__previous_run__case=case)
        )
        .exclude(result_detail_projections())
    )
    if filters.get("history") != "all":
        latest = (
            AnalysisRun.objects.filter(
                case=case,
                result__case=case,
                status__in=SUCCESSFUL,
                equivalence_key=OuterRef("result__run__equivalence_key"),
            )
            .filter(Q(previous_run__isnull=True) | Q(previous_run__case=case))
            .order_by("-created_at", "-pk")
        )
        queryset = queryset.filter(result__run_id=Subquery(latest.values("pk")[:1]))
    if filters.get("weak") != "1":
        queryset = queryset.exclude(TIMING_ONLY_FINDINGS)
    if query := filters.get("q"):
        queryset = queryset.filter(search_text__icontains=query)
    if method := filters.get("method"):
        queryset = queryset.filter(result__run__operation=method)
    if labels is not None:
        queryset = queryset.filter(label__in=labels)
    person = person or filters.get("person")
    if person and person.case_id != case.pk:
        raise Http404("Person not found.")
    if person or filters.get("kind") or filters.get("evidence"):
        sources = AnalysisFindingSource.objects.filter(
            case=case, finding_id=OuterRef("pk")
        )
        if kind := filters.get("kind"):
            sources = sources.filter(kind=kind)
        if evidence := filters.get("evidence"):
            sources = sources.filter(evidence_id=evidence)
        if person:
            related = Q(pk__in=[])
            for kind in ("person-location", "text-message", "phone-call", "facetime"):
                related |= Q(
                    kind=kind,
                    evidence_id__in=person_evidence(
                        scoped_evidence(case, kind), person
                    ).values("pk"),
                )
            sources = sources.filter(related)
        queryset = queryset.filter(Exists(sources))
    return queryset.order_by(
        "-result__run__created_at", "-result__run_id", "item_index", "pk"
    )


def finding_rows(case, findings, *, show_weak=False):
    """Resolve only the page, without fetching canonical result payloads."""
    from core.analysis.services import (
        SourceResolutionCache,
        require_current_geocode_reviews,
        resolve_inputs,
    )

    findings = list(findings)
    runs = {
        run.pk: run
        for run in AnalysisRun.objects.filter(
            case=case,
            pk__in={finding.run_id for finding in findings},
        ).only(
            "case_id",
            "operation",
            "inputs",
            "parameters",
            "status",
            "method_version",
            "created_at",
        )
    }
    cache = SourceResolutionCache(case, with_content=False)
    try:
        cache.prepare([source for run in runs.values() for source in run.inputs])
    except Http404, ValueError, TypeError, KeyError, forms.ValidationError:
        # A corrupt historical reference must not prevent valid neighbouring rows.
        cache = SourceResolutionCache(case, with_content=False)
    references = {}
    for reference in AnalysisFindingSource.objects.filter(
        case=case, finding__in=findings
    ):
        references.setdefault(reference.finding_id, {})[str(reference.source_id)] = (
            reference
        )
    checked = {}
    rows = []
    for finding in findings:
        run = runs[finding.run_id]
        if run.pk not in checked:
            try:
                sources = resolve_inputs(
                    run, case, require_current_review=False, cache=cache
                )
                warning = ""
                try:
                    require_current_geocode_reviews(run, sources)
                except AnalysisError:
                    warning = (
                        "An address estimate used by this run is no longer selected."
                    )
                checked[run.pk] = (
                    {str(source.id): source for source in sources},
                    warning,
                )
            except AnalysisError, Http404:
                checked[run.pk] = (None, "")
        sources, warning = checked[run.pk]
        cited = references.get(finding.pk, {})
        unavailable = sources is None or not cited or not cited.keys() <= sources.keys()
        if not unavailable:
            unavailable = any(
                (reference.kind, reference.evidence_id, reference.attachment_id)
                != (
                    sources[key].kind,
                    sources[key].item.pk,
                    sources[key].attachment.pk if sources[key].attachment else None,
                )
                for key, reference in cited.items()
            )
        row = {
            "id": finding.pk,
            "result_id": finding.result_id,
            "item_index": finding.item_index,
            "label": "Finding unavailable" if unavailable else finding.label,
            "excerpt": "" if unavailable else finding.excerpt,
            "preview": "" if unavailable else finding.preview,
            "url": finding_url(
                case.pk,
                run.pk,
                finding.item_index,
                show_weak=show_weak or finding.weak,
            ),
            "method": OPERATIONS[run.operation].label
            if run.operation in OPERATIONS
            else run.operation,
            "created_at": run.created_at,
            "uses_ai": bool(requires_inference(run.operation, run.parameters)),
            "weak": finding.weak,
            "weak_label": (
                "Timing only"
                if run.operation == "conversation_link"
                else "Weak plate region"
            )
            if finding.weak
            else "",
            "partial": run.status == "partial",
            "unavailable": unavailable,
            "warning": warning,
            "review_reason": ""
            if unavailable
            else getattr(finding, "review_reason", ""),
            "sources": [],
            "additional_sources": max(0, len(cited) - 3),
        }
        if not unavailable:
            row["participant_names"] = (
                participant_names(
                    source for key, source in sources.items() if key in cited
                )
                if row["uses_ai"]
                else {}
            )
            row["preview"] = participant_text(row["preview"], row["participant_names"])
            row["label"] = participant_text(row["label"], row["participant_names"])
            row["excerpt"] = participant_text(row["excerpt"], row["participant_names"])
            row["sources"] = [
                {
                    "source_id": key,
                    "title": source.item.title,
                    "url": source.original_url,
                    "kind": KIND_LABELS[source.kind],
                    "archived": source.item.is_archived,
                    "id": source.item.pk,
                    "sha256": source.attachment.sha256
                    if source.attachment
                    else source.item.sha256,
                }
                for key, source in sources.items()
                if key in cited
            ]
        rows.append(row)
    return rows


def catalogue_page(case, params, *, person=None, page_parameter="page", labels=None):
    form = CatalogueForm(params, case=case)
    valid = form.is_valid()
    filters = form.cleaned_data if valid else {}
    all_findings = (
        finding_queryset(case, filters | {"weak": "1"}, person=person, labels=labels)
        if valid
        else AnalysisFinding.objects.none()
    )
    timing_only_count = (
        all_findings.filter(TIMING_ONLY_FINDINGS).count() if valid else 0
    )
    show_timing_only = filters.get("weak") == "1"
    queryset = (
        all_findings
        if show_timing_only or not valid
        else all_findings.exclude(TIMING_ONLY_FINDINGS)
    )
    if filters.get("evidence"):
        queryset = rank_findings(queryset, case)
    queryset = queryset.annotate(
        excerpt=content_excerpt("search_text", filters["q"])
        if filters.get("q")
        else F("preview"),
        run_id=F("result__run_id"),
    ).only("id", "case_id", "result_id", "item_index", "label", "weak", "preview")
    page = Paginator(queryset, PER_PAGE).get_page(params.get(page_parameter))
    page.object_list = finding_rows(case, page.object_list, show_weak=show_timing_only)
    query = {
        key: value for key, value in params.items() if key in form.fields and value
    }
    prefix = "?" + urlencode(query) + ("&" if query else "") + page_parameter + "="
    visibility_query = query.copy()
    if show_timing_only:
        visibility_query.pop("weak", None)
    else:
        visibility_query["weak"] = "1"
    evidence_title = ""
    if evidence := filters.get("evidence"):
        for kind in [filters["kind"]] if filters.get("kind") else KINDS:
            evidence_title = (
                scoped_evidence(case, kind)
                .filter(pk=evidence)
                .values_list("title", flat=True)
                .first()
                or ""
            )
            if evidence_title:
                break
        evidence_title = evidence_title or "Unavailable selected evidence"
    return {
        "form": form,
        "filters": filters,
        "page_obj": page,
        "rows": page.object_list,
        "timing_only_count": timing_only_count,
        "show_timing_only": show_timing_only,
        "timing_visibility_url": "?" + urlencode(visibility_query),
        "previous_url": prefix + str(page.previous_page_number())
        if page.has_previous()
        else "",
        "next_url": prefix + str(page.next_page_number()) if page.has_next() else "",
        "evidence_title": evidence_title,
        "advanced_filters": bool(form.errors)
        or any(
            filters.get(key) for key in ("method", "kind", "person", "history", "weak")
        ),
    }
