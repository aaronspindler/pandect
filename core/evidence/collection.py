"""Paginated evidence collections shared by readers and person records."""

from django.core.paginator import Paginator
from django.db.models import Case, CharField, Q, Value, When
from django.urls import reverse

from core.analysis.search import derived_content_condition, derived_text_matches
from core.evidence.navigation import collection_params
from core.evidence.queries import person_evidence, scoped_evidence, selected_person
from core.evidence.search import retained_content_condition, retained_text_matches
from core.evidence.types import (
    COMMUNICATION_KINDS,
    FILE_KINDS,
    KIND_LABELS,
    KINDS,
    LOCATION_KINDS,
)
from people.models import Person


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


def collection_context(case, params):
    query = params.get("q", "")[:200].strip()
    checksum = params.get("checksum", "")[:64]
    selected_kind = params.get("type", "")
    if selected_kind not in KINDS:
        selected_kind = ""
    state = params.get("state", "active")
    if state not in {"active", "archived", "all"}:
        state = "active"
    person = selected_person(case, params.get("person"))
    queries = []
    for kind in KINDS:
        if selected_kind and kind != selected_kind:
            continue
        queryset = scoped_evidence(case, kind)
        if checksum:
            queryset = queryset.filter(sha256=checksum)
        if person:
            queryset = person_evidence(queryset, person)
        if state != "all":
            queryset = queryset.filter(is_archived=state == "archived")
        if query:
            condition = (
                Q(title__icontains=query)
                | Q(description__icontains=query)
                | Q(source__icontains=query)
                | Q(source_description__icontains=query)
                | Q(source_url__icontains=query)
            )
            if kind in FILE_KINDS:
                condition |= Q(original_filename__icontains=query)
            if kind in LOCATION_KINDS:
                condition |= Q(address__icontains=query)
            if kind in COMMUNICATION_KINDS:
                for participant in ("sender", "recipient"):
                    for field in ("first_name", "last_name", "email", "phone_number"):
                        condition |= Q(**{f"{participant}__{field}__icontains": query})
            queryset, content_condition = retained_content_condition(
                queryset, case=case, kind=kind, query=query
            )
            condition |= content_condition
            queryset, extracted_condition = derived_content_condition(
                queryset, case=case, kind=kind, query=query
            )
            condition |= extracted_condition
            if kind == "person-location":
                condition |= (
                    Q(person__first_name__icontains=query)
                    | Q(person__last_name__icontains=query)
                    | Q(person__email__icontains=query)
                    | Q(person__phone_number__icontains=query)
                )
            if kind == "vehicle-location":
                condition |= (
                    Q(vehicle__make__icontains=query)
                    | Q(vehicle__model__icontains=query)
                    | Q(vehicle__license_plate__icontains=query)
                    | Q(vehicle__vin__icontains=query)
                    | Q(raw_row__Asset__icontains=query)
                    | Q(**{"raw_row__Serial Number__icontains": query})
                )
            queryset = queryset.filter(condition)
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
    params = collection_params(params)
    suffix = "?" + params.urlencode() if params else ""
    rows = []
    for row in page.object_list:
        row["label"] = KIND_LABELS[row["kind"]]
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
        rows.append(row)
    matches = derived_text_matches(case=case, rows=rows, query=query)
    matches.update(retained_text_matches(case=case, rows=rows, query=query))
    for row in rows:
        row["content_match"] = matches.get((row["kind"], row["id"]))
        if row["content_match"] and row["content_match"].get("url"):
            row["url"] = row["content_match"]["url"]
        if row["content_match"] and row["content_match"].get("anchor"):
            row["url"] += "#" + row["content_match"]["anchor"]
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
        "checksum": checksum,
        "selected_kind": selected_kind,
        "state": state,
        "selected_person": person,
        "person_choices": Person.objects.filter(case=case),
        "kind_choices": KIND_LABELS.items(),
        "list_suffix": suffix,
        "list_url": reverse("core:evidence:list", kwargs={"case_id": case.pk}) + suffix,
    }
