"""Case-shared selections and deliberate reuse of their retained references."""

from urllib.parse import urlencode

from django import forms
from django.contrib import messages
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_safe

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.analysis.errors import AnalysisError, error_message
from core.analysis.form_base import selection as parse_source
from core.analysis.form_base import source_choices
from core.analysis.forms import analysis_form
from core.analysis.operations import OPERATIONS
from core.analysis.readiness import source_readiness
from core.analysis.selections import (
    append_member,
    build_brief,
    check_revision,
    finding_member,
    member_key,
    resolve_member,
    resolve_selection,
    revision_token,
    selection_choices,
    source_member,
)
from core.analysis.services import snapshot_sources, start_run
from core.evidence.collection import collection_context
from core.models import AnalysisResult, SavedSelection

MEMBER_SALT = "core.selection.add-member"


class SelectionForm(forms.Form):
    name = forms.CharField(max_length=160, label="Selection name")
    revision = forms.CharField(widget=forms.HiddenInput, required=False)


class RevisionForm(forms.Form):
    revision = forms.CharField(widget=forms.HiddenInput)


class AddForm(RevisionForm):
    member = forms.CharField(widget=forms.HiddenInput)


class DeleteForm(RevisionForm):
    confirm = forms.BooleanField(
        label="Delete this selection; retain its evidence and findings"
    )


def _context(request, case):
    return case_context(case, request.user) | {"nav_section": "selections"}


def _get(case, selection_id, *, lock=False):
    queryset = SavedSelection.objects.filter(case=case)
    if lock:
        queryset = queryset.select_for_update()
    return get_object_or_404(queryset, pk=selection_id)


def _save(selection, actor):
    selection.updated_by = actor
    selection.revision += 1
    selection.full_clean()
    selection.save()


def _target_params(request):
    return {
        key: request.GET[key]
        for key in ("source", "kind", "evidence", "attachment", "result", "item")
        if key in request.GET
    }


def _target(case, params):
    try:
        if params.get("source"):
            return source_member(case, parse_source(params["source"]))
        if params.get("kind") and params.get("evidence"):
            choice = {"kind": params["kind"], "id": params["evidence"]}
            if params.get("attachment"):
                choice["attachment_id"] = params["attachment"]
            if params.get("result"):
                choice["result_id"] = params["result"]
            return source_member(case, choice)
        if params.get("result") and "item" in params:
            return finding_member(case, params["result"], int(params["item"]))
    except (
        ValidationError,
        AnalysisError,
        AnalysisResult.DoesNotExist,
        KeyError,
        IndexError,
        ValueError,
        TypeError,
    ):
        raise Http404("Retained source or finding not found.") from None
    raise Http404("Choose a retained source or finding.")


@product_login_required
@require_safe
def selection_list(request, case_id):
    case = get_case(request.user, case_id)
    query = request.GET.get("q", "")[:200].strip()
    selections = SavedSelection.objects.filter(case=case)
    if query:
        selections = selections.filter(name__icontains=query)
    return render(
        request,
        "core/analysis/selection_list.html",
        {
            **_context(request, case),
            "query": query,
            "page_obj": Paginator(selections, 25).get_page(request.GET.get("page")),
        },
    )


@product_login_required
@require_http_methods(["GET", "POST"])
def selection_create(request, case_id):
    with transaction.atomic():
        case = get_case(request.user, case_id, edit=True, lock=request.method == "POST")
        form = SelectionForm(request.POST if request.method == "POST" else None)
        if request.method == "POST" and form.is_valid():
            selection = SavedSelection(
                case=case,
                name=form.cleaned_data["name"],
                created_by=request.user,
                updated_by=request.user,
            )
            selection.full_clean()
            selection.save(force_insert=True)
            target = _target_params(request)
            if target:
                return redirect(
                    reverse("core:analysis:selection_add", args=[case.pk, selection.pk])
                    + "?"
                    + urlencode(target)
                )
            return redirect(
                "core:analysis:selection_detail",
                case_id=case.pk,
                selection_id=selection.pk,
            )
    return render(
        request,
        "core/analysis/selection_form.html",
        {
            **_context(request, case),
            "form": form,
            "creating": True,
        },
        status=400 if request.method == "POST" else 200,
    )


@product_login_required
@require_safe
def selection_detail(request, case_id, selection_id):
    case = get_case(request.user, case_id)
    selection = _get(case, selection_id)
    resolved = resolve_selection(selection, case, excerpt_only=True)
    return render(
        request,
        "core/analysis/selection_detail.html",
        {
            **_context(request, case),
            "selection": selection,
            **resolved,
        },
    )


@product_login_required
@require_http_methods(["GET", "POST"])
def selection_edit(request, case_id, selection_id):
    stale = False
    with transaction.atomic():
        case = get_case(request.user, case_id, edit=True, lock=request.method == "POST")
        selection = _get(case, selection_id, lock=request.method == "POST")
        form = SelectionForm(
            request.POST if request.method == "POST" else None,
            initial={"name": selection.name, "revision": revision_token(selection)},
        )
        if request.method == "POST" and form.is_valid():
            try:
                check_revision(selection, form.cleaned_data["revision"])
            except ValidationError as error:
                stale = True
                form.add_error(None, error)
                form.data = form.data.copy()
                form.data["revision"] = revision_token(selection)
            else:
                selection.name = form.cleaned_data["name"]
                _save(selection, request.user)
                return redirect(
                    "core:analysis:selection_detail",
                    case_id=case.pk,
                    selection_id=selection.pk,
                )
    return render(
        request,
        "core/analysis/selection_form.html",
        {
            **_context(request, case),
            "form": form,
            "selection": selection,
            "stale": stale,
        },
        status=409 if stale else 400 if request.method == "POST" else 200,
    )


@product_login_required
@require_safe
def selection_pick(request, case_id):
    case = get_case(request.user, case_id, edit=True)
    params = _target_params(request)
    member = _target(case, params)
    row = resolve_member(member, case)
    selections = Paginator(SavedSelection.objects.filter(case=case), 25).get_page(
        request.GET.get("page")
    )
    for selection in selections:
        selection.add_url = (
            reverse("core:analysis:selection_add", args=[case.pk, selection.pk])
            + "?"
            + urlencode(params)
        )
    return render(
        request,
        "core/analysis/selection_pick.html",
        {
            **_context(request, case),
            "row": row,
            "page_obj": selections,
            "create_url": reverse("core:analysis:selection_create", args=[case.pk])
            + "?"
            + urlencode(params),
        },
    )


@product_login_required
@require_http_methods(["GET", "POST"])
def selection_add(request, case_id, selection_id):
    stale = False
    variants = []
    with transaction.atomic():
        case = get_case(request.user, case_id, edit=True, lock=request.method == "POST")
        selection = _get(case, selection_id, lock=request.method == "POST")
        params = _target_params(request)
        if request.method == "GET" and not params:
            context = collection_context(case, request.GET)
            for row in context["rows"]:
                row["url"] = (
                    reverse("core:analysis:selection_add", args=[case.pk, selection.pk])
                    + "?"
                    + urlencode({"kind": row["kind"], "evidence": row["id"]})
                )
            return render(
                request,
                "core/analysis/selection_sources.html",
                {
                    **_context(request, case),
                    **context,
                    "selection": selection,
                },
            )
        row = None
        if request.method == "GET":
            member = _target(case, params)
            row = resolve_member(member, case)
            if row["type"] == "source":
                source = row["source"]
                variant_operation = (
                    "entities"
                    if source.kind in OPERATIONS["entities"].kinds
                    else "geocode_locations"
                )
                variants = [
                    {
                        "label": label,
                        "url": reverse(
                            "core:analysis:selection_add", args=[case.pk, selection.pk]
                        )
                        + "?"
                        + urlencode({"source": value}),
                    }
                    for value, label in source_choices(
                        case, variant_operation, evidence=(source.kind, source.item)
                    )
                    if value.count(":") > 1
                ]
            form = AddForm(
                initial={
                    "revision": revision_token(selection),
                    "member": signing.dumps(
                        {
                            "case": str(case.pk),
                            "selection": str(selection.pk),
                            "reference": member,
                        },
                        salt=MEMBER_SALT,
                    ),
                }
            )
        else:
            form = AddForm(request.POST)
            if form.is_valid():
                try:
                    check_revision(selection, form.cleaned_data["revision"])
                    signed = signing.loads(
                        form.cleaned_data["member"], salt=MEMBER_SALT
                    )
                    if signed["case"] != str(case.pk) or signed["selection"] != str(
                        selection.pk
                    ):
                        raise ValidationError(
                            "Choose this source again from the selection."
                        )
                    member = signed["reference"]
                    row = resolve_member(member, case)
                    added = append_member(selection, member)
                    if added:
                        _save(selection, request.user)
                except (
                    ValidationError,
                    AnalysisError,
                    signing.BadSignature,
                    AnalysisResult.DoesNotExist,
                    KeyError,
                    ValueError,
                    TypeError,
                ) as error:
                    stale = True
                    form.add_error(
                        None,
                        error
                        if isinstance(error, ValidationError)
                        else "This reference is unavailable. "
                        "Choose it again from its source.",
                    )
                else:
                    messages.success(
                        request,
                        "Added to selection."
                        if added
                        else "This exact reference is already in the selection.",
                    )
                    return redirect(
                        "core:analysis:selection_detail",
                        case_id=case.pk,
                        selection_id=selection.pk,
                    )
    return render(
        request,
        "core/analysis/selection_add.html",
        {
            **_context(request, case),
            "selection": selection,
            "row": row,
            "form": form,
            "variants": variants,
        },
        status=409 if stale else 400 if request.method == "POST" else 200,
    )


@product_login_required
@require_http_methods(["GET", "POST"])
def selection_remove(request, case_id, selection_id, member_index):
    with transaction.atomic():
        case = get_case(request.user, case_id, edit=True, lock=request.method == "POST")
        selection = _get(case, selection_id, lock=request.method == "POST")
        if member_index >= len(selection.members):
            raise Http404("Selection member not found.")
        form = RevisionForm(
            request.POST if request.method == "POST" else None,
            initial={"revision": revision_token(selection)},
        )
        if request.method == "POST" and form.is_valid():
            try:
                check_revision(selection, form.cleaned_data["revision"])
            except ValidationError as error:
                form.add_error(None, error)
            else:
                selection.members = [
                    value
                    for index, value in enumerate(selection.members)
                    if index != member_index
                ]
                _save(selection, request.user)
                return redirect(
                    "core:analysis:selection_detail",
                    case_id=case.pk,
                    selection_id=selection.pk,
                )
    return render(
        request,
        "core/analysis/selection_remove.html",
        {
            **_context(request, case),
            "selection": selection,
            "form": form,
            "member_number": member_index + 1,
        },
        status=409 if request.method == "POST" else 200,
    )


@product_login_required
@require_http_methods(["GET", "POST"])
def selection_delete(request, case_id, selection_id):
    with transaction.atomic():
        case = get_case(request.user, case_id, edit=True, lock=request.method == "POST")
        selection = _get(case, selection_id, lock=request.method == "POST")
        form = DeleteForm(
            request.POST if request.method == "POST" else None,
            initial={"revision": revision_token(selection)},
        )
        if request.method == "POST" and form.is_valid():
            try:
                check_revision(selection, form.cleaned_data["revision"])
            except ValidationError as error:
                form.add_error(None, error)
            else:
                selection.delete()
                return redirect("core:analysis:selection_list", case_id=case.pk)
    return render(
        request,
        "core/analysis/selection_delete.html",
        {
            **_context(request, case),
            "selection": selection,
            "form": form,
        },
        status=409 if request.method == "POST" else 200,
    )


@product_login_required
@require_safe
def selection_brief(request, case_id, selection_id):
    case = get_case(request.user, case_id)
    selection = _get(case, selection_id)
    try:
        html = build_brief(selection, case)
    except ValidationError as error:
        return render(
            request,
            "core/analysis/selection_export_error.html",
            {
                **_context(request, case),
                "selection": selection,
                "errors": error.messages,
            },
            status=409,
        )
    response = HttpResponse(html, content_type="text/html; charset=utf-8")
    response["Content-Disposition"] = (
        f'attachment; filename="selection-{selection.pk}.html"'
    )
    response["Cache-Control"] = "private, no-store"
    response["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; "
        "script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"
    )
    return response


def _encoded_choice(choice):
    value = f"{choice['kind']}:{choice['id']}"
    if choice.get("attachment_id") or choice.get("result_id"):
        value += ":" + (choice.get("attachment_id") or "-")
    if choice.get("result_id"):
        value += ":" + choice["result_id"]
    if choice.get("geocode_candidate") is not None:
        value += ":" + str(choice["geocode_candidate"])
    return value


@product_login_required
@require_http_methods(["GET", "POST"])
def selection_analyze(request, case_id, selection_id):
    form, errors = None, []
    with transaction.atomic():
        case = get_case(request.user, case_id, edit=True, lock=request.method == "POST")
        selection = _get(case, selection_id, lock=request.method == "POST")
        resolved = resolve_selection(selection, case, require_current_review=True)
        operation = (
            request.POST.get("operation", "")
            if request.method == "POST"
            else request.GET.get("operation", "")
        )
        operations = {
            key: value
            for key, value in OPERATIONS.items()
            if key not in {"question_synthesis", "imessage_import"}
        }
        try:
            choices = selection_choices(resolved)
            operations = {
                key: spec
                for key, spec in operations.items()
                if choices and all(choice["kind"] in spec.kinds for choice in choices)
            }
            if not operations:
                raise ValidationError(
                    "No method accepts every source in this selection. "
                    "Create a smaller compatible selection."
                )
            if operation in operations:
                form = analysis_form(
                    request.POST if request.method == "POST" else None,
                    case=case,
                    operation=operation,
                )
                options = [
                    (_encoded_choice(choice), row["title"])
                    for choice, row in zip(choices, resolved["members"], strict=True)
                ]
                form.fields["sources"].choices = options
                form.fields["sources"].disabled = True
                form.initial["sources"] = [value for value, _ in options]
                form.readiness = source_readiness(case, operation, options)
                form.fields["selection_revision"] = forms.CharField(
                    widget=forms.HiddenInput, initial=revision_token(selection)
                )
                if request.method == "POST" and form.is_valid():
                    check_revision(selection, form.cleaned_data["selection_revision"])
                    current = snapshot_sources(
                        case, choices, allowed_kinds=operations[operation].kinds
                    )
                    if [member_key(value) for value in current] != [
                        member_key(row["member"]["source"])
                        for row in resolved["members"]
                    ]:
                        raise ValidationError(
                            "A retained source or approval changed. "
                            "Review the selection before starting analysis."
                        )
                    run, _ = start_run(
                        request.user,
                        case.pk,
                        operation,
                        choices,
                        parameters=form.parameters(),
                        submission_id=form.cleaned_data["submission_id"],
                        external_processing=form.cleaned_data["external_processing"],
                    )
                    return redirect(
                        "core:analysis:detail", case_id=case.pk, run_id=run.pk
                    )
            elif request.method == "POST":
                raise ValidationError("Choose an available analysis method.")
        except (ValidationError, AnalysisError) as error:
            errors = (
                error.messages
                if isinstance(error, ValidationError)
                else [error_message(error.code)]
            )
    return render(
        request,
        "core/analysis/selection_analysis.html",
        {
            **_context(request, case),
            "selection": selection,
            "resolved": resolved,
            "form": form,
            "errors": errors,
            "operations": operations,
            "operation": operation,
        },
        status=400 if request.method == "POST" else 200,
    )
