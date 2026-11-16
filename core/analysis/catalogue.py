"""Complete, case-scoped browsing of retained finding projections."""

from itertools import batched
from urllib.parse import urlencode

from django import forms
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Exists, F, OuterRef, Q, Subquery
from django.http import Http404

from core.analysis.errors import AnalysisError
from core.analysis.execution import requires_inference
from core.analysis.navigation import finding_url
from core.analysis.operations import OPERATIONS
from core.analysis.projection import finding_values
from core.evidence.queries import person_evidence, scoped_evidence
from core.evidence.search import content_excerpt
from core.evidence.types import KIND_LABELS, KINDS
from core.models import (
    AnalysisFinding,
    AnalysisFindingSource,
    AnalysisReview,
    AnalysisRun,
)
from people.models import Person

SUCCESSFUL = ("complete", "partial", "empty")
PER_PAGE = 25


@transaction.atomic
def project_result(result):
    """Publish a rebuildable projection atomically alongside its canonical result."""
    inputs = {source["id"]: source for source in result.run.inputs}
    for chunk in batched(enumerate(result.data.get("items", [])), 500, strict=False):
        findings = [
            AnalysisFinding(
                case_id=result.case_id,
                result=result,
                item_index=index,
                **finding_values(result.run.operation, item),
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
        label="Person", queryset=Person.objects.none(), required=False
    )
    review = forms.ChoiceField(
        label="Finding review",
        required=False,
        choices=[
            ("", "All review states"),
            ("unreviewed", "Unreviewed"),
            *AnalysisReview.Decision.choices,
        ],
    )
    history = forms.ChoiceField(
        label="Run versions",
        required=False,
        choices=[
            ("", "Latest equivalent successful runs"),
            ("all", "All retained runs"),
        ],
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
    """Return filtered items before pagination; review never revives an old run."""
    queryset = AnalysisFinding.objects.filter(
        case=case,
        result__case=case,
        result__run__case=case,
        result__run__status__in=SUCCESSFUL,
    ).filter(
        Q(result__run__previous_run__isnull=True)
        | Q(result__run__previous_run__case=case)
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
    reviews = AnalysisReview.objects.filter(
        case=case,
        result_id=OuterRef("result_id"),
        item_index=OuterRef("item_index"),
    ).order_by("-created_at", "pk")
    whole_reviews = AnalysisReview.objects.filter(
        case=case,
        result_id=OuterRef("result_id"),
        item_index__isnull=True,
    ).order_by("-created_at", "pk")
    queryset = queryset.annotate(
        review=Subquery(reviews.values("decision")[:1]),
        result_review=Subquery(whole_reviews.values("decision")[:1]),
    )
    if query := filters.get("q"):
        queryset = queryset.filter(search_text__icontains=query)
    if method := filters.get("method"):
        queryset = queryset.filter(result__run__operation=method)
    if labels is not None:
        queryset = queryset.filter(label__in=labels)
    if review := filters.get("review"):
        queryset = (
            queryset.filter(review__isnull=True)
            if review == "unreviewed"
            else queryset.filter(review=review)
        )
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


def finding_rows(case, findings):
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
                    warning = "The location review has changed since this run."
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
            "url": finding_url(
                case.pk, run.pk, finding.item_index, show_weak=finding.weak
            ),
            "method": OPERATIONS[run.operation].label
            if run.operation in OPERATIONS
            else run.operation,
            "created_at": run.created_at,
            "uses_ai": bool(requires_inference(run.operation, run.parameters)),
            "review": finding.review or "unreviewed",
            "result_review": finding.result_review,
            "weak": finding.weak,
            "partial": run.status == "partial",
            "unavailable": unavailable,
            "warning": warning,
            "sources": [],
            "additional_sources": max(0, len(cited) - 3),
        }
        if not unavailable:
            row["sources"] = [
                {
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
    queryset = (
        finding_queryset(case, filters, person=person, labels=labels)
        if valid
        else AnalysisFinding.objects.none()
    )
    queryset = queryset.annotate(
        excerpt=content_excerpt("search_text", filters["q"])
        if filters.get("q")
        else F("preview"),
        run_id=F("result__run_id"),
    ).only("id", "case_id", "result_id", "item_index", "label", "weak")
    page = Paginator(queryset, PER_PAGE).get_page(params.get(page_parameter))
    page.object_list = finding_rows(case, page.object_list)
    query = {
        key: value for key, value in params.items() if key != page_parameter and value
    }
    prefix = "?" + urlencode(query) + ("&" if query else "") + page_parameter + "="
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
        "previous_url": prefix + str(page.previous_page_number())
        if page.has_previous()
        else "",
        "next_url": prefix + str(page.next_page_number()) if page.has_next() else "",
        "evidence_title": evidence_title,
        "advanced_filters": bool(form.errors)
        or any(
            filters.get(key)
            for key in ("method", "kind", "person", "review", "history")
        ),
    }
