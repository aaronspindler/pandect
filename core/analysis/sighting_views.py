"""Private case pages for composing and reviewing sighting groups."""

from uuid import UUID, uuid4

from django import forms
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import CharField, Exists, OuterRef, Value
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import (
    require_http_methods,
    require_POST,
    require_safe,
)

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.analysis.errors import AnalysisError
from core.analysis.important import has_available_citations, retained_finding_url
from core.analysis.navigation import finding_url
from core.analysis.services import (
    SourceResolutionCache,
    validate_current_sighting,
)
from core.analysis.sightings import (
    ANCHOR_KINDS,
    MAX_CANDIDATES,
    MAX_SELECTION,
    REVIEW_DECISIONS,
    candidate_links,
    review_outcome,
    review_sighting,
    saved_group,
    sighting_reviews,
    sighting_runs,
    start_sighting,
)
from core.evidence.queries import get_evidence, scoped_evidence
from core.evidence.types import KIND_LABELS
from core.important.navigation import return_params
from core.important.reader import reader_context
from core.models import AnalysisFindingSource, AnalysisRun


class SightingForm(forms.Form):
    anchor = forms.CharField(max_length=80, widget=forms.HiddenInput)
    submission_id = forms.UUIDField(initial=uuid4, widget=forms.HiddenInput)
    links = forms.Field(
        label="Direct connections",
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    def clean_links(self):
        links = self.cleaned_data["links"]
        if len(links) > MAX_SELECTION or any(len(link) > 160 for link in links):
            raise ValidationError("Choose a smaller selection of direct connections.")
        return links


class ReviewForm(forms.Form):
    revision = forms.UUIDField(required=False)
    decision = forms.ChoiceField(choices=REVIEW_DECISIONS)


def _source_choice(case, token, kinds):
    kind, _, raw_id = token.partition(":")
    if kind not in kinds:
        raise Http404("Observation not found.")
    try:
        source_id = UUID(raw_id)
    except ValueError:
        raise Http404("Observation not found.") from None
    source = get_evidence(case, kind, source_id)
    if source.is_archived:
        raise Http404("Active observation not found.")
    return {"kind": kind, "id": source.pk, "title": source.title}


def _evidence_page(case, kinds, query, page):
    sources = []
    for kind in kinds:
        queryset = scoped_evidence(case, kind).filter(is_archived=False).order_by()
        if query:
            queryset = queryset.filter(title__icontains=query)
        sources.append(
            queryset.annotate(kind=Value(kind, output_field=CharField())).values(
                "kind", "id", "title", "created_at"
            )
        )
    merged = (
        sources[0].union(*sources[1:], all=True).order_by("-created_at", "kind", "id")
    )
    return Paginator(merged, 25).get_page(page)


def _picker_row(source):
    kind = source["kind"]
    return source | {"token": f"{kind}:{source['id']}", "label": KIND_LABELS[kind]}


def _context_selection(data):
    selected = {
        key.removeprefix("context_"): role
        for key, role in data.items()
        if key.startswith("context_") and role in {"supporting", "contrary"}
    }
    if any(len(token) > 80 for token in selected) or any(
        list(selected.values()).count(role) > MAX_SELECTION
        for role in ("supporting", "contrary")
    ):
        raise ValidationError(f"Select up to {MAX_SELECTION} records in each role.")
    return selected


def _context_rows(case, page, selected):
    page_rows = [_picker_row(source) for source in page]
    rows = {row["token"]: row for row in page_rows}
    for token in sorted(selected.keys() - rows.keys()):
        try:
            rows[token] = _picker_row(_source_choice(case, token, KIND_LABELS))
        except Http404:
            # Keep the user's draft visible without exposing an unavailable source.
            rows[token] = {
                "token": token,
                "title": "Unavailable record",
                "label": "Clear this selection before saving",
            }
    return [row | {"role": selected.get(token, "")} for token, row in rows.items()]


def _original_url(case, kind, evidence_id):
    return reverse("core:evidence:detail", args=(case.pk, kind, evidence_id))


def _endpoint(case, source):
    finding = source.get("finding")
    return source | {
        "original_url": _original_url(case, source["kind"], source["evidence_id"]),
        "finding_url": finding_url(case.pk, finding["run_id"], finding["item_index"])
        if finding
        else "",
        "timestamp": parse_datetime(source["observed_at"])
        if source.get("observed_at")
        else None,
    }


def _display_links(case, links, selected=()):
    return [
        link
        | {
            "endpoints": [_endpoint(case, source) for source in link["endpoints"]],
            "selected": link.get("token") in selected,
        }
        for link in links
    ]


@product_login_required
@require_safe
def index(request, case_id):
    case = get_case(request.user, case_id)
    page = Paginator(sighting_runs(case).select_related("result"), 25).get_page(
        request.GET.get("page")
    )
    rows = _group_rows(case, page)
    return render(
        request,
        "core/analysis/sighting_groups.html",
        case_context(case, request.user) | {"page": page, "rows": rows},
    )


@product_login_required
@require_http_methods(["GET", "HEAD", "POST"])
def create(request, case_id):
    case = get_case(request.user, case_id, edit=True)
    data = request.POST if request.method == "POST" else request.GET
    anchor_token = data.get("anchor", "")
    context = case_context(case, request.user)
    if not anchor_token:
        if request.method == "POST":
            raise Http404("Observation not found.")
        query = data.get("q", "").strip()[:200]
        page = _evidence_page(case, ANCHOR_KINDS, query, data.get("page"))
        return render(
            request,
            "core/analysis/sighting_choose.html",
            context
            | {"page": page, "observations": map(_picker_row, page), "q": query},
        )
    anchor = _source_choice(case, anchor_token, ANCHOR_KINDS)
    is_post = request.method == "POST"
    form = SightingForm(
        request.POST if is_post else None, initial={"anchor": anchor_token}
    )
    selected = {}
    browse_context = data.get("browse_context") if is_post else None
    if is_post:
        try:
            selected = _context_selection(data)
            if form.is_valid() and browse_context is None:
                run = start_sighting(
                    request.user,
                    case.pk,
                    anchor_token,
                    form.cleaned_data["links"],
                    selected,
                    submission_id=form.cleaned_data["submission_id"],
                )
                return redirect("core:analysis:sighting_detail", case.pk, run.pk)
        except ValidationError as error:
            form.add_error(None, error)
        except AnalysisError, Http404:
            form.add_error(
                None,
                "A selection is unavailable. Choose an active record or comparison.",
            )
    try:
        candidates = candidate_links(case, anchor_token)
    except ValidationError, AnalysisError, Http404:
        candidates = []
    query = data.get("context_query", "").strip()[:200] if is_post else ""
    page = _evidence_page(case, tuple(KIND_LABELS), query, browse_context)
    return render(
        request,
        "core/analysis/sighting_create.html",
        context
        | {
            "anchor": anchor,
            "anchor_url": _original_url(case, anchor["kind"], anchor["id"]),
            "form": form,
            "links": _display_links(case, candidates, form["links"].value() or ()),
            "context_page": page,
            "context_records": _context_rows(case, page, selected),
            "context_open": bool(browse_context or selected),
            "context_query": query,
            "candidate_limit": MAX_CANDIDATES,
            "selection_limit": MAX_SELECTION,
        },
        status=400 if form.errors else 200,
    )


def _group_rows(case, runs, *, hide_unavailable=False):
    cache = SourceResolutionCache(case, with_content=False, active_only=True)
    rows = []
    for run in runs:
        result = getattr(run, "result", None)
        title, status = "Sighting group", run.get_status_display()
        description = ""
        if result is not None or run.status == AnalysisRun.Status.COMPLETE:
            try:
                _, values, _ = saved_group(case, run, cache=cache)
            except AnalysisError:
                if hide_unavailable:
                    continue
                status = "Proposal unavailable"
            else:
                title = values["anchor"]["title"]
                description = values["links"][0]["endpoints"][1]["title"]
                if remaining := len(values["links"]) - 1:
                    description += f" and {remaining} more"
                status = "Saved proposal"
        rows.append(
            {"run": run, "title": title, "description": description, "status": status}
        )
    return rows


def _alternative_page(case, run, values, number):
    endpoint_ids = {
        endpoint["evidence_id"]
        for link in values["links"]
        for endpoint in link["endpoints"]
    }
    original_matches = AnalysisFindingSource.objects.filter(
        case=case,
        finding__case=case,
        finding__result_id=OuterRef("result__pk"),
        evidence_id__in=endpoint_ids,
    )
    alternatives = (
        sighting_runs(case)
        .exclude(equivalence_key=run.equivalence_key)
        .filter(
            Exists(original_matches),
            result__case=case,
            result__data__items__0__values__member_identities__has_any_keys=values[
                "member_identities"
            ],
        )
        .select_related("result")
    )
    page = Paginator(alternatives, 25).get_page(number)
    page.object_list = _group_rows(case, page, hide_unavailable=True)
    return page


def _detail(request, case, run, *, issue="", status=200):
    context = case_context(case, request.user) | {"run": run, "issue": issue}
    if not hasattr(run, "result") and run.status != AnalysisRun.Status.COMPLETE:
        return redirect("core:analysis:detail", case.pk, run.pk)
    try:
        result, values, sources = saved_group(
            case,
            run,
            cache=SourceResolutionCache(case, with_content=False, active_only=True),
        )
    except AnalysisError:
        context["unavailable"] = True
    else:
        context |= _saved_detail(request, case, run, result, values, sources)
        source_map = {str(source.id): source for source in sources}
        if has_available_citations(run.operation, result.data["items"][0], source_map):
            context.update(
                reader_context(
                    request,
                    case,
                    finding_result=result,
                    item_index=0,
                    source_url=retained_finding_url(case.pk, run, 0),
                )
            )
    return render(request, "core/analysis/sighting_detail.html", context, status=status)


def _saved_detail(request, case, run, result, values, sources):
    stale_code = ""
    try:
        validate_current_sighting(run, case, sources)
    except AnalysisError as error:
        stale_code = error.code
    reviews = sighting_reviews(case, result).select_related("reviewed_by")
    latest = reviews.first()
    history = Paginator(reviews, 25).get_page(request.GET.get("reviews"))
    supporting = [_endpoint(case, source) for source in values["supporting"]]
    contrary = [_endpoint(case, source) for source in values["contrary"]]
    return {
        "result": result,
        "stale_code": stale_code,
        "anchor": _endpoint(case, values["anchor"]),
        "links": _display_links(case, values["links"]),
        "supporting": supporting,
        "contrary": contrary,
        "alternatives": _alternative_page(
            case, run, values, request.GET.get("alternatives")
        ),
        "revision": latest.pk if latest else "",
        "outcome": review_outcome(latest),
        "history": history,
        "history_rows": [(entry, review_outcome(entry)) for entry in history],
        "retry_id": uuid4(),
        "review_url": reverse(
            "core:analysis:sighting_review",
            args=[case.pk, run.pk],
            query=return_params(request.GET),
        ),
    }


@product_login_required
@require_safe
def detail(request, case_id, run_id):
    case = get_case(request.user, case_id)
    return _detail(request, case, get_object_or_404(sighting_runs(case), pk=run_id))


@product_login_required
@require_POST
def review(request, case_id, run_id):
    form = ReviewForm(request.POST)
    case, run, issue_code = review_sighting(
        request.user, case_id, run_id, **(form.cleaned_data if form.is_valid() else {})
    )
    if not issue_code:
        return redirect(
            retained_finding_url(case.pk, run, 0, history=return_params(request.GET))
        )
    issue, status = {
        "invalid_decision": ("Choose a valid review decision.", 400),
        "review_changed": (
            "Someone reviewed this proposal. Check the latest decision.",
            409,
        ),
        "source_changed": (
            "A source is no longer current. Review the retained history.",
            409,
        ),
    }[issue_code]
    return _detail(request, case, run, issue=issue, status=status)
