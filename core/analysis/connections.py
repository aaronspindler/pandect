"""Exact candidate strings in retained analysis, never inferred identities."""

import re

from django.core.paginator import Paginator
from django.db.models import CharField, Count, F, OuterRef, Q, Subquery, Value
from django.db.models.functions import Cast, Coalesce, Concat, Substr
from django.http import Http404
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_safe

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.analysis.catalogue import CatalogueForm, finding_queryset, finding_rows
from core.models import AnalysisFindingSource

IDENTIFIERS = {
    "email": "Email",
    "phone": "Phone candidate",
    "url": "URL",
    "domain": "Domain candidate",
    "identifier": "Identifier candidate",
    "plate": "Plate-text candidate",
}


def _page_rows(case, queryset, number):
    page = Paginator(
        queryset.annotate(
            run_id=F("result__run_id"),
            excerpt=F("preview"),
        ).only("id", "case_id", "result_id", "item_index", "label", "weak", "preview"),
        25,
    ).get_page(number)
    page.object_list = finding_rows(case, page.object_list)
    return page


@product_login_required
@require_safe
def connections(request, case_id):
    case = get_case(request.user, case_id)
    form = CatalogueForm(request.GET, case=case)
    valid = form.is_valid()
    filters = form.cleaned_data if valid else {}
    findings = finding_queryset(case, filters).exclude(match_kind="")
    if not valid:
        findings = findings.none()
    # Count exact original/attachment identities, not extraction versions or runs.
    identity = Concat(
        F("sources__kind"),
        Value(":"),
        Cast("sources__evidence_id", CharField()),
        Value(":"),
        Coalesce(Cast("sources__attachment_id", CharField()), Value("")),
    )
    sample = findings.filter(
        match_kind=OuterRef("match_kind"),
        match_digest=OuterRef("match_digest"),
        match_version=OuterRef("match_version"),
    ).values("pk")[:1]
    groups = (
        findings.filter(sources__case=case)
        .order_by()
        .values(
            "match_kind",
            "match_digest",
            "match_version",
        )
        .annotate(
            original_count=Count(identity, distinct=True),
            checksum_count=Count("sources__source_sha256", distinct=True),
            partial_count=Count(
                "pk", filter=Q(result__run__status="partial"), distinct=True
            ),
            sample_id=Subquery(sample),
        )
        .filter(original_count__gte=2)
        .order_by("-original_count", "match_kind", "match_digest")
    )
    context = case_context(case, request.user) | {
        "nav_section": "connections",
        "form": form,
        "filters": filters,
    }
    digest = request.GET.get("key", "")
    kind = request.GET.get("identifier", "")
    if digest:
        if not re.fullmatch(r"[a-f0-9]{64}", digest) or kind not in IDENTIFIERS:
            raise Http404("Connection not found.")
        group = groups.filter(match_kind=kind, match_digest=digest).first()
        if group is None:
            raise Http404("Connection not found.")
        matching = findings.filter(
            match_kind=kind, match_digest=digest, match_version=group["match_version"]
        )
        # Keep the newest occurrence per original, regardless of retained reruns.
        originals = (
            AnalysisFindingSource.objects.filter(case=case, finding__in=matching)
            .order_by(
                "kind",
                "evidence_id",
                "attachment_id",
                "-finding__result__run__created_at",
                "-finding__result__run_id",
                "finding__item_index",
            )
            .distinct("kind", "evidence_id", "attachment_id")
            .values("finding_id")
        )
        page = _page_rows(
            case, matching.filter(pk__in=Subquery(originals)), request.GET.get("page")
        )
        context |= {
            "page_obj": page,
            "rows": page.object_list,
            "connection": group,
            "identifier_label": IDENTIFIERS[kind],
        }
    else:
        page = Paginator(groups, 25).get_page(request.GET.get("page"))
        retained = list(page.object_list)
        samples = (
            findings.filter(pk__in=[group["sample_id"] for group in retained])
            .annotate(
                run_id=F("result__run_id"),
                excerpt=Substr("match_key", 1, 300),
            )
            .only(
                "id", "case_id", "result_id", "item_index", "label", "weak", "preview"
            )
        )
        checked = {row["id"]: row for row in finding_rows(case, samples)}
        for group in retained:
            row = checked[group["sample_id"]]
            query = request.GET.copy()
            query.pop("page", None)
            query.update(
                {"key": group["match_digest"], "identifier": group["match_kind"]}
            )
            group |= {
                "label": IDENTIFIERS[group["match_kind"]],
                "value": row["excerpt"],
                "unavailable": row["unavailable"],
                "duplicate_checksums": group["checksum_count"]
                < group["original_count"],
                "url": reverse("core:analysis:connections", kwargs={"case_id": case.pk})
                + "?"
                + query.urlencode(),
            }
        page.object_list = retained
        context |= {"page_obj": page, "groups": retained}
    response = render(request, "core/analysis/connections.html", context)
    response["Cache-Control"] = "private, no-store"
    return response
