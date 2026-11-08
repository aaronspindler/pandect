from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from cases.permissions import case_context, get_case
from config.access import product_login_required
from people.models import Person
from people.views import list_query
from socialmedia.forms import (
    SocialAccountDeleteForm,
    SocialAccountForm,
    social_account_revision,
)
from socialmedia.models import SocialAccount


def person_accounts_url(request, person):
    url = reverse(
        "people:detail", kwargs={"case_id": person.case_id, "person_id": person.pk}
    )
    if query := list_query(request):
        url += f"?{query}"
    return f"{url}#social-accounts"


@product_login_required
@require_http_methods(["GET", "POST"])
@never_cache
def social_account_save(request, case_id, person_id, account_id=None):
    latest_account = None
    with transaction.atomic():
        case = get_case(request.user, case_id, edit=True, lock=request.method == "POST")
        person = get_object_or_404(Person, case=case, pk=person_id)
        if account_id is None:
            account = SocialAccount(person=person)
        else:
            accounts = person.social_accounts.all()
            if request.method == "POST":
                accounts = accounts.select_for_update()
            account = get_object_or_404(accounts, pk=account_id)
        form = SocialAccountForm(
            request.POST if request.method == "POST" else None, instance=account
        )
        if request.method == "POST" and form.is_valid():
            form.save(commit=False).save(
                force_insert=account_id is None, force_update=account_id is not None
            )
            messages.success(
                request,
                "Social account saved." if account_id else "Social account added.",
            )
            return redirect(person_accounts_url(request, person))
        if form.stale:
            latest_account = person.social_accounts.get(pk=account_id)
            form.data = form.data.copy()
            form.data["revision"] = form.initial["revision"]
    return render(
        request,
        "socialmedia/social_account_form.html",
        {
            **case_context(case, request.user),
            "person": person,
            "account": account,
            "form": form,
            "creating": account_id is None,
            "latest_account": latest_account,
            "return_url": person_accounts_url(request, person),
            "list_query": list_query(request),
        },
        status=409 if form.stale else 400 if request.method == "POST" else 200,
    )


@product_login_required
@require_http_methods(["GET", "POST"])
@never_cache
def social_account_delete(request, case_id, person_id, account_id):
    with transaction.atomic():
        case = get_case(request.user, case_id, edit=True, lock=request.method == "POST")
        person = get_object_or_404(Person, case=case, pk=person_id)
        accounts = person.social_accounts.all()
        if request.method == "POST":
            accounts = accounts.select_for_update()
        account = get_object_or_404(accounts, pk=account_id)
        revision = social_account_revision(account)
        submitted = request.POST if request.method == "POST" else None
        stale = (
            submitted is not None and submitted.get("revision", revision) != revision
        )
        form = SocialAccountDeleteForm(
            submitted if not stale else None, initial={"revision": revision}
        )
        if form.is_valid():
            account.delete()
            messages.success(request, "Social account removed.")
            return redirect(person_accounts_url(request, person))
    return render(
        request,
        "socialmedia/social_account_delete.html",
        {
            **case_context(case, request.user),
            "person": person,
            "account": account,
            "form": form,
            "stale": stale,
            "return_url": person_accounts_url(request, person),
        },
        status=409 if stale else 400 if request.method == "POST" else 200,
    )
