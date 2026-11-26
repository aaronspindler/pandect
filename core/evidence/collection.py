"""Paginated evidence collections shared by readers and person records."""

from django.core.paginator import Paginator
from django.db.models import Case, CharField, Value, When
from django.urls import reverse

from core.case_search.discovery import discover
from core.evidence.navigation import collection_params
from core.evidence.queries import (
    matching_checksum,
    person_evidence,
    scoped_evidence,
    selected_person,
)
from core.evidence.types import (
    COMMUNICATION_KINDS,
    KIND_LABELS,
    KINDS,
)
from core.evidence.workspace_rows import attach_workspace_rows
from people.models import Person
from phone.presentation import message_labels


def person_attribution(kind, person):
    if person and kind in COMMUNICATION_KINDS:
        return Case(
            When(sender=person, recipient=person, then=Value("Both")),
            When(sender=person, then=Value("Sender")),
            When(recipient=person, then=Value("Recipient")),
            default=Value(""),
            output_field=CharField(),
        )
    return Value(
        "Location" if person and kind == "person-location" else "",
        output_field=CharField(),
    )


def collection_context(case, params, *, actor=None, workspace_rows=False):
    query = params.get("q", "")[:200].strip()
    checksum = params.get("checksum", "")[:64]
    selected_kind = params.get("type", "")
    if selected_kind not in KINDS:
        selected_kind = ""
    state = params.get("state", "active")
    if state not in {"active", "archived", "all"}:
        state = "active"
    person = selected_person(case, params.get("person"))
    results = discover(case, actor, query, kind=selected_kind)
    queries = []
    for kind in KINDS:
        if selected_kind and kind != selected_kind:
            continue
        queryset = scoped_evidence(case, kind)
        if checksum:
            queryset = matching_checksum(queryset, checksum)
        if person:
            queryset = person_evidence(queryset, person)
        if state != "all":
            queryset = queryset.filter(is_archived=state == "archived")
        if query:
            queryset = queryset.filter(pk__in=results.evidence[kind])
        queries.append(
            queryset.order_by()
            .annotate(
                kind=Value(kind, output_field=CharField()),
                association=person_attribution(kind, person),
            )
            .values(
                "id",
                "title",
                "created_at",
                "is_archived",
                "kind",
                "sha256",
                "association",
            )
        )
    combined = (
        queries[0].union(*queries[1:], all=True).order_by("-created_at", "kind", "id")
    )
    page = Paginator(combined, 25).get_page(params.get("page"))
    rows = list(page.object_list)
    if workspace_rows:
        attach_workspace_rows(case, rows)
    else:
        message_ids = [row["id"] for row in rows if row["kind"] == "text-message"]
        message_titles = message_labels(case, message_ids)
        for row in rows:
            if row["kind"] == "text-message":
                row["title"] = message_titles.get(row["id"], row["title"])
    params = collection_params(params)
    suffix = "?" + params.urlencode() if params else ""
    for row in rows:
        row["label"] = KIND_LABELS[row["kind"]]
        row["content_match"] = results.excerpts.get((row["kind"], str(row["id"])))
        row["url"] = (
            reverse(
                "core:evidence:detail",
                kwargs={
                    "case_id": case.pk,
                    "kind": row["kind"],
                    "evidence_id": row["id"],
                },
            )
            + suffix
        )
    pagination = {}
    for name, number in (
        ("previous", page.previous_page_number() if page.has_previous() else None),
        ("next", page.next_page_number() if page.has_next() else None),
    ):
        if number is not None:
            page_params = params.copy()
            page_params["page"] = number
            pagination[name] = "?" + page_params.urlencode()
    return {
        "rows": rows,
        "page_obj": page,
        "pagination": pagination,
        "query": query,
        "search_notice": results.notice,
        "checksum": checksum,
        "selected_kind": selected_kind,
        "state": state,
        "selected_person": person,
        "person_choices": Person.objects.filter(case=case),
        "kind_choices": KIND_LABELS.items(),
        "list_suffix": suffix,
        "list_url": reverse("core:evidence:list", kwargs={"case_id": case.pk}) + suffix,
    }
