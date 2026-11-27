"""Case-scoped selection, interpretation, and review for plan comparisons."""

import json
from uuid import uuid4

from django.conf import settings
from django.contrib import messages
from django.core import signing
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse

from cases.permissions import case_context
from core.analysis.errors import AnalysisError, error_message
from core.analysis.form_base import _source_choice_page
from core.analysis.navigation import current_comparison_origin, evidence_return_url
from core.analysis.readiness import source_readiness
from geo.analysis.deterministic.journey_claims import (
    claimed_subjects,
    validate_comparison,
)
from geo.journey_claim_checks import COMPARISON_EXTRACTIONS
from geo.journey_claim_forms import STATUSES, InterpretationForm
from geo.journey_claim_inputs import derived_page, expand_input
from geo.journey_claim_requests import (
    DERIVED_INPUT_PREFIXES,
    comparison_selection,
    queue_comparison,
)

PICKERS = {"claim": "content_events", "movement": "location_conflicts"}


def _picker(case, actor, role, data, chosen, *, recover=False):
    """Use the existing bounded cursor picker for originals and extractions."""
    action = data.get("browse", "")
    query = data.get(role + "_query", "")[:200]
    cursor = data.get(role + "_cursor", "")
    backwards = data.get(role + "_back") == "1"
    derived_number = data.get(role + "_derived_page", "1")
    if action == role + "-search":
        cursor, backwards, derived_number = "", False, 1
    elif action in (role + "-next", role + "-previous"):
        backwards = action.endswith("-previous")
        cursor = data.get(role + ("_before" if backwards else "_after"), "")
    if action in (role + "-derived-next", role + "-derived-previous"):
        try:
            derived_number = max(
                1, int(derived_number) + (1 if action.endswith("-next") else -1)
            )
        except ValueError:
            derived_number = 1

    raw = [value for value in chosen if not value.startswith(DERIVED_INPUT_PREFIXES)]
    from core.case_search.discovery import discover

    results = discover(case, actor, query)
    page_options = {
        "selected": raw,
        "extractions_by_kind": COMPARISON_EXTRACTIONS,
        "matched": results.evidence,
    }
    reset = False
    try:
        page, retained = _source_choice_page(
            case, PICKERS[role], query, cursor=cursor, reverse=backwards, **page_options
        )
    except Http404:
        if not cursor:
            raise
        cursor, backwards, reset = "", False, True
        page, retained = _source_choice_page(case, PICKERS[role], query, **page_options)
    choices = dict(retained) | dict(page)
    readiness = (
        source_readiness(case, PICKERS[role], choices.items()) if choices else {}
    )
    derived = derived_page(
        case, role, page=derived_number, query=query, results=results
    )
    rows = []
    for token, label in choices.items():
        state = readiness[token]
        rows.append(
            (token, label, state["label"] if state["blocking"] else "", token in chosen)
        )
    unavailable = any(
        token not in choices or readiness[token]["blocking"] for token in raw
    )
    derived_choices = {token: (label, blocking) for token, label, blocking in derived}
    for token in chosen:
        if not token.startswith(DERIVED_INPUT_PREFIXES):
            continue
        if token in derived_choices and (not recover or derived_choices[token][1]):
            continue
        try:
            option = expand_input(case, token, role)
        except AnalysisError, ValidationError:
            derived_choices.pop(token, None)
            unavailable = True
        else:
            derived_choices.setdefault(token, (option["label"], ""))
    return {
        "role": role,
        "notice": results.notice,
        "rows": rows,
        "derived_rows": [
            (token, label, blocking, token in chosen and not blocking)
            for token, (label, blocking) in derived_choices.items()
        ],
        "derived_open": derived.number > 1
        or any(token in derived_choices for token in chosen),
        "page": page,
        "derived": derived,
        "cursor": cursor,
        "back": backwards,
        "query": query,
        "reset": reset,
        "unavailable": unavailable,
    }


def _draft_options(options, claims):
    """Every chosen statement source must supply an interpretation or reference."""
    used_sources = {
        span["source_id"]
        for claim in claims
        for span in [claim, *claim.get("citations", []), claim.get("reference")]
        if span
    }
    used_findings = {
        (claim["result_id"], claim["item_index"])
        for claim in claims
        if "result_id" in claim
    }
    return [
        key
        for key, option in options.items()
        if (
            (option["claim"]["result_id"], option["claim"]["item_index"])
            not in used_findings
            if option["claim"]
            else option["source"].id not in used_sources
        )
    ]


def _review_rows(claims, sources, subjects):
    def wording(anchor):
        if anchor:
            return sources[anchor["source_id"]].text[anchor["start"] : anchor["end"]]
        return ""

    return [
        {
            "claim": claim,
            "source": sources[claim["source_id"]],
            "quote": wording(claim),
            "status": dict(STATUSES)[claim["status"]],
            "subjects": [
                subjects.get(key, "Unavailable") for key in claimed_subjects(claim)
            ],
            "reference": wording(claim.get("reference")),
            "applies_to": wording(claim.get("applies_to")),
        }
        for claim in claims
    ]


def _interpret(request, case, state, context, action, salt):
    _, sources, options, parameters, digest = comparison_selection(
        case, state["picked"]
    )
    if state.get("digest", digest) != digest:
        raise ValidationError(
            "A selected source or trip changed. Choose the sources again."
        )
    state["digest"] = digest
    missing = _draft_options(options, state["claims"])
    option_key = request.POST.get("option") or next(iter(missing or options))
    if option_key not in options:
        raise Http404("Passage source unavailable.")
    option = options[option_key]
    by_id = {source.id: source for source in sources}
    references = {row["source"].id: row["source"] for row in options.values()}
    subjects = {
        f"person:{p.pk}": str(p)
        for p in case.people.only("first_name", "last_name", "phone_number", "email")
    } | {f"vehicle:{v.pk}": str(v) for v in case.vehicles.defer("notes")}
    claims = state["claims"]
    form = InterpretationForm(
        request.POST if action == "review" else None,
        subjects=subjects,
        option=option,
        references=references,
        claims=_review_rows(claims, by_id, subjects),
    )
    review = False
    if "remove" in request.POST:
        try:
            index = int(request.POST["remove"])
            if not 0 <= index < len(claims):
                raise ValueError
        except ValueError:
            raise Http404("Interpretation unavailable.") from None
        remaining = claims[:index] + claims[index + 1 :]
        try:
            if remaining:
                validate_comparison(sources, parameters | {"claims": remaining})
        except ValidationError:
            context["error"] = "Remove the linked cancellation or correction first."
        else:
            state["claims"] = remaining
        review = bool(state["claims"])
    if action == "review" and form.is_valid():
        candidate = claims + [form.cleaned_data["claim"]]
        try:
            validate_comparison(
                sources, parameters | {"claims": candidate}, check_size=True
            )
            if len(json.dumps(parameters | {"claims": candidate}).encode()) > 32768:
                raise ValidationError(
                    "The comparison settings are too large. Narrow the selection."
                )
        except ValidationError as error:
            form.add_error(None, error)
        else:
            state["claims"] = candidate
            review = True
    missing = _draft_options(options, state["claims"])
    state["reviewed"] = review and not missing
    context.update(
        draft=signing.dumps(state, salt=salt, compress=True),
        option_key=option_key,
        options=options.items(),
        option_count=len(options),
        option=option,
        claims=_review_rows(state["claims"], by_id, subjects),
        form=form,
        missing=[options[key]["label"] for key in missing],
        observations=[by_id[key] for key in parameters["observations"]],
        groups=parameters["groups"],
    )
    template = (
        "geo/journey_claim_review.html" if review else "geo/journey_claim_edit.html"
    )
    return render(request, template, context)


def _queue(request, case, state):
    run, created = queue_comparison(request.user, case.pk, state)
    messages.success(
        request,
        "Comparison queued." if created else "This comparison was already queued.",
    )
    return redirect(
        reverse(
            "core:analysis:detail",
            args=(case.pk, run.pk),
            query=state["result_query"],
        )
    )


def create(request, case, *, chooser_query=None, result_query=None):
    """Called from the verified, edit-authorized Additional analysis chooser."""
    data = request.POST if request.method == "POST" else request.GET
    context = case_context(case, request.user) | {"nav_section": "analysis"}
    salt = f"journey-claims.{case.pk}.{request.user.pk}"
    action = request.POST.get("action", "")
    token = request.POST.get("draft")
    if token:
        try:
            state = signing.loads(token, salt=salt)
        except signing.BadSignature:
            raise Http404("Comparison selection unavailable.") from None
    else:
        state = {
            "picked": {
                role: list(dict.fromkeys(request.POST.getlist(role)))
                for role in PICKERS
            },
            "claims": [],
            "submission_id": str(uuid4()),
        }
    # New drafts keep the validated origin. Older drafts use the current
    # chooser context and can still complete with no originating record.
    state.setdefault("chooser_query", chooser_query or {})
    state.setdefault("result_query", result_query or {})
    if token:
        state["chooser_query"], state["result_query"] = current_comparison_origin(
            case, state["chooser_query"], state["result_query"]
        )
    chooser = state["chooser_query"]
    context.update(
        chooser_url=reverse("core:analysis:create", args=(case.pk,), query=chooser),
        picker_url=reverse(
            "core:analysis:create",
            args=(case.pk,),
            query=chooser | {"operation": "journey_claims"},
        ),
        draft_url=reverse("core:analysis:create", args=(case.pk,)),
        cancel_url=evidence_return_url(case.pk, state["result_query"])
        or reverse("core:analysis:index", args=(case.pk,)),
    )
    try:
        if token:
            if action == "queue":
                return _queue(request, case, state)
            if action != "choose_again":
                return _interpret(request, case, state, context, action, salt)
            data = state.get("picker_state", {})
        limit = settings.PANDECT_ANALYSIS_MAX_SOURCES
        if sum(map(len, state["picked"].values())) > limit:
            raise ValidationError(
                f"Choose at most {limit} sources, including trip members."
            )
        pickers = context["pickers"] = [
            _picker(
                case,
                request.user,
                role,
                data,
                state["picked"][role],
                recover=action == "choose_again" or bool(data.get("browse")),
            )
            for role in PICKERS
        ]
        context["search_notice"] = next(
            (p["notice"] for p in pickers if p["notice"]), ""
        )
        notices = []
        if any(picker["unavailable"] for picker in pickers):
            if action == "prepare":
                raise ValidationError(
                    "A selected source is unavailable. "
                    "Choose its exact retained material."
                )
            notices.append("Unavailable sources were removed from the selection.")
        if any(picker["reset"] for picker in pickers):
            notices.append(
                "A source page is no longer available. "
                "Its search restarted from the first page."
            )
        if notices:
            context["error"] = " ".join(notices)
        if action == "prepare":
            if not all(state["picked"].values()):
                raise ValidationError(
                    "Select a statement source and an observation, trip, or visit."
                )
            state["picker_state"] = {
                f"{picker['role']}_{key}": value
                for picker in context["pickers"]
                for key, value in (
                    ("query", picker["query"]),
                    ("cursor", picker["cursor"]),
                    ("back", "1" if picker["back"] else ""),
                    ("derived_page", picker["derived"].number),
                )
            }
            return _interpret(request, case, state, context, action, salt)
    except (AnalysisError, ValidationError) as error:
        context["error"] = (
            error_message(error.code)
            if isinstance(error, AnalysisError)
            else " ".join(error.messages)
        )
        if token and action != "choose_again":
            context["restart_draft"] = token
    return render(request, "geo/journey_claim_choose.html", context)
