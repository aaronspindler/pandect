"""Choose one exact retained quotation and queue its approved comparison."""

from django import forms
from django.contrib import messages
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.analysis.errors import AnalysisError, error_message
from core.analysis.execution import selected_ai_executions
from core.analysis.manual import start_available_run
from core.analysis.navigation import current_comparison_origin, evidence_return_url
from core.analysis.sources import selection
from core.evidence.types import KIND_LABELS
from phone.analysis.presentation import addition_label
from text.analysis.claim_comparison import MAX_QUOTE, selectable_claim
from text.claim_comparison_requests import quote_offsets, review_sources

OPERATION = "claim_comparison"


class ClaimForm(forms.Form):
    source_id = forms.ChoiceField(label="Claim source")
    quote = forms.CharField(
        label="Exact quotation",
        max_length=MAX_QUOTE,
        strip=False,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="Copy one statement from the claim source, including any qualifiers.",
    )

    def __init__(self, data=None, *, claim_sources):
        super().__init__(data)
        self.claim_sources = claim_sources
        choices = [(key, row["label"]) for key, row in claim_sources.items()]
        if len(choices) == 1:
            self.initial["source_id"] = choices[0][0]
        else:
            choices.insert(0, ("", "Choose a source"))
        self.fields["source_id"].choices = choices

    def clean(self):
        data = super().clean()
        source_id, quote = data.get("source_id"), data.get("quote")
        if source_id and quote:
            try:
                start, end = quote_offsets(
                    self.claim_sources[source_id]["source"].text, quote
                )
            except ValidationError as error:
                self.add_error("quote", error)
            else:
                data["claim"] = {"source_id": source_id, "start": start, "end": end}
        return data


def _comparison_rows(sources):
    rows = {}
    claim_sources = {}
    for source in sources:
        label = f"{KIND_LABELS[source.kind]} · {source.item.title}"
        if occurred_at := getattr(source.item, "occurred_at", None):
            label += f" · {timezone.localtime(occurred_at):%d %b %Y %H:%M %Z}"
        addition = addition_label(source, OPERATION)
        if addition:
            label += f" · {addition}"
        if source.derived_run_id:
            label += f" · Extraction {source.derived_run_id[:8]}"
        row = {"source": source, "label": label}
        rows[source.id] = row
        if selectable_claim(source):
            claim_sources[source.id] = row
    return rows, claim_sources


def _salt(case, user):
    return f"text.claim-comparison.{case.pk}.{user.pk}"


def _render(request, case, *, draft_data, **context):
    return render(
        request,
        "text/claim_comparison.html",
        case_context(case, request.user)
        | {
            "nav_section": "analysis",
            "tokens": draft_data["tokens"],
            "submission_id": draft_data["submission_id"],
            "picker_state": draft_data.get("picker_state", {}),
            "picker_url": reverse(
                "core:analysis:create",
                args=[case.pk],
                query=draft_data.get("chooser_query", {}) | {"operation": OPERATION},
            ),
            "cancel_url": evidence_return_url(
                case.pk, draft_data.get("result_query", {})
            )
            or reverse("core:analysis:index", args=[case.pk]),
        }
        | context,
        status=400 if context.get("error") else 200,
    )


def prepare(
    request,
    case,
    tokens,
    *,
    submission_id,
    chooser_query=None,
    result_query=None,
    picker_state=None,
):
    """Open the quotation step from the editor-authorized evidence picker."""
    try:
        digest, sources = review_sources(case, [selection(value) for value in tokens])
    except Http404:
        raise AnalysisError("source_unavailable") from None
    rows, claim_sources = _comparison_rows(sources)
    draft = {
        "digest": digest,
        "revision": str(case.external_ai_revision),
        "tokens": tokens,
        "submission_id": str(submission_id),
        "chooser_query": chooser_query or {},
        "result_query": result_query or {},
        "picker_state": picker_state or {},
    }
    return _render(
        request,
        case,
        draft_data=draft,
        form=ClaimForm(claim_sources=claim_sources),
        draft=signing.dumps(draft, salt=_salt(case, request.user)),
        rows=rows.values(),
        claim_sources=claim_sources.values(),
    )


@product_login_required
@require_POST
@transaction.atomic
def confirm(request, case_id):
    case = get_case(request.user, case_id, edit=True, lock=True)
    token = request.POST.get("draft", "")
    try:
        draft = signing.loads(token, salt=_salt(case, request.user))
    except signing.BadSignature:
        raise Http404("Comparison selection unavailable.") from None
    chooser_query = draft.get("chooser_query", {})
    current_chooser, result_query = current_comparison_origin(
        case, chooser_query, draft.get("result_query", {})
    )
    if current_chooser != chooser_query:
        draft["picker_state"] = {"q": draft.get("picker_state", {}).get("q", "")}
    draft.update(chooser_query=current_chooser, result_query=result_query)
    selections = [selection(value) for value in draft["tokens"]]
    try:
        _, sources = review_sources(case, selections, expected_digest=draft["digest"])
    except (AnalysisError, ValidationError, Http404) as error:
        message = (
            " ".join(error.messages)
            if isinstance(error, ValidationError)
            else error_message(
                error.code if isinstance(error, AnalysisError) else "source_unavailable"
            )
        )
        return _render(request, case, draft_data=draft, error=message)

    rows, claim_sources = _comparison_rows(sources)
    form = ClaimForm(request.POST, claim_sources=claim_sources)
    if form.is_valid():
        try:
            route = selected_ai_executions(case, OPERATION, {}).get("gateway")
            if not route or draft.get("revision") != str(case.external_ai_revision):
                raise AnalysisError("external_processing_not_approved")
            run, created = start_available_run(
                request.user,
                case,
                OPERATION,
                selections,
                parameters={"claim": form.cleaned_data["claim"], "backend": "gateway"},
                execution=route,
                submission_id=draft["submission_id"],
            )
        except (AnalysisError, ValidationError) as error:
            form.add_error(
                None,
                error_message(error.code)
                if isinstance(error, AnalysisError)
                else error,
            )
        else:
            messages.success(
                request,
                "Claim comparison queued."
                if created
                else "This comparison was already queued.",
            )
            return redirect(
                reverse(
                    "core:analysis:detail",
                    args=[case.pk, run.pk],
                    query=draft.get("result_query"),
                )
            )
    return _render(
        request,
        case,
        draft_data=draft,
        form=form,
        draft=token,
        rows=rows.values(),
        claim_sources=claim_sources.values(),
    )
