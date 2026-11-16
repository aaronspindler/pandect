from urllib.parse import urlencode

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.db.models.deletion import RestrictedError
from django.http import QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_safe

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.analysis.catalogue import catalogue_page
from core.evidence.collection import collection_context
from people.forms import PersonDeleteForm, PersonForm
from people.models import Person
from socialmedia.models import SocialAccount


def list_query(request):
    values = {}
    query = request.GET.get("q", "").strip()[:200]
    if query:
        values["q"] = query
    if request.GET.get("page", "").isdigit():
        values["page"] = request.GET["page"]
    return urlencode(values)


@product_login_required
@require_safe
def person_list(request, case_id):
    case = get_case(request.user, case_id)
    query = request.GET.get("q", "").strip()[:200]
    people = Person.objects.filter(case=case)
    if query:
        matching_accounts = SocialAccount.objects.filter(
            person_id=OuterRef("pk")
        ).filter(Q(platform__icontains=query) | Q(username__icontains=query))
        people = people.filter(
            Q(first_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(phone_number__icontains=query)
            | Q(email__icontains=query)
            | Q(address__icontains=query)
            | Q(notes__icontains=query)
            | Exists(matching_accounts)
        )
    page = Paginator(people, 25).get_page(request.GET.get("page"))
    return render(
        request,
        "people/list.html",
        {
            **case_context(case, request.user),
            "page_obj": page,
            "query": query,
            "search_query": urlencode({"q": query}) if query else "",
            "list_query": list_query(request),
        },
    )


@product_login_required
@require_safe
def person_detail(request, case_id, person_id):
    case = get_case(request.user, case_id)
    person = get_object_or_404(Person, case=case, pk=person_id)
    evidence_params = QueryDict(mutable=True)
    evidence_params.update({"person": str(person.pk), "state": "all"})
    evidence_url = (
        reverse("core:evidence:list", args=[case.pk])
        + "?"
        + evidence_params.urlencode()
    )
    evidence_params["page"] = request.GET.get("evidence_page", "1")
    related_evidence = collection_context(case, evidence_params)
    finding_params = QueryDict(mutable=True)
    finding_params["findings_page"] = request.GET.get("findings_page", "1")
    related_findings = catalogue_page(
        case, finding_params, person=person, page_parameter="findings_page"
    )
    return render(
        request,
        "people/detail.html",
        {
            **case_context(case, request.user),
            "person": person,
            "social_accounts": person.social_accounts.all(),
            "related_evidence": related_evidence,
            "related_findings": related_findings,
            "related_evidence_url": evidence_url,
            "related_findings_url": reverse("core:analysis:browse", args=[case.pk])
            + "?"
            + urlencode({"person": str(person.pk)}),
            "poi_page": Paginator(person.points_of_interest.all(), 25).get_page(
                request.GET.get("poi_page")
            ),
            "list_query": list_query(request),
        },
    )


@product_login_required
@require_http_methods(["GET", "POST"])
def person_create(request, case_id):
    with transaction.atomic():
        case = get_case(request.user, case_id, edit=True, lock=request.method == "POST")
        person = Person(case=case)
        form = PersonForm(
            request.POST if request.method == "POST" else None, instance=person
        )
        if request.method == "POST" and form.is_valid():
            person = form.save(commit=False)
            person.case = case
            person.save(force_insert=True)
            messages.success(request, "Person added.")
            url = reverse(
                "people:detail", kwargs={"case_id": case.pk, "person_id": person.pk}
            )
            if query := list_query(request):
                url += f"?{query}"
            return redirect(url)
    return render(
        request,
        "people/form.html",
        {
            **case_context(case, request.user),
            "form": form,
            "creating": True,
            "list_query": list_query(request),
        },
        status=400 if request.method == "POST" else 200,
    )


@product_login_required
@require_http_methods(["GET", "POST"])
def person_edit(request, case_id, person_id):
    latest_person = None
    with transaction.atomic():
        case = get_case(request.user, case_id, edit=True, lock=request.method == "POST")
        people = Person.objects.filter(case=case)
        if request.method == "POST":
            people = people.select_for_update()
        person = get_object_or_404(people, pk=person_id)
        form = PersonForm(
            request.POST if request.method == "POST" else None, instance=person
        )
        if request.method == "POST" and form.is_valid():
            form.save(commit=False).save(force_update=True)
            messages.success(request, "Person details saved.")
            url = reverse(
                "people:detail", kwargs={"case_id": case.pk, "person_id": person.pk}
            )
            if query := list_query(request):
                url += f"?{query}"
            return redirect(url)
        if form.stale:
            latest_person = Person.objects.get(pk=person.pk, case=case)
            form.data = form.data.copy()
            form.data["revision"] = form.initial["revision"]
    return render(
        request,
        "people/form.html",
        {
            **case_context(case, request.user),
            "form": form,
            "person": person,
            "latest_person": latest_person,
            "creating": False,
            "list_query": list_query(request),
        },
        status=409 if form.stale else 400 if request.method == "POST" else 200,
    )


@product_login_required
@require_http_methods(["GET", "POST"])
def person_delete(request, case_id, person_id):
    with transaction.atomic():
        case = get_case(
            request.user, case_id, owner=True, lock=request.method == "POST"
        )
        people = Person.objects.filter(case=case)
        if request.method == "POST":
            people = people.select_for_update()
        person = get_object_or_404(people, pk=person_id)
        form = PersonDeleteForm(request.POST if request.method == "POST" else None)
        if request.method == "POST" and form.is_valid():
            try:
                person.delete()
            except RestrictedError:
                form.add_error(
                    None,
                    "This person is linked to retained location evidence or "
                    "communications and cannot "
                    "be deleted individually. Archived evidence also retains its link.",
                )
            else:
                messages.success(request, "Person record deleted.")
                url = reverse("people:list", kwargs={"case_id": case.pk})
                if query := list_query(request):
                    url += f"?{query}"
                return redirect(url)
    return render(
        request,
        "people/delete.html",
        {
            **case_context(case, request.user),
            "person": person,
            "form": form,
            "list_query": list_query(request),
        },
        status=400 if request.method == "POST" else 200,
    )
