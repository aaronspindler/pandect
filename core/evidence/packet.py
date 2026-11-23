"""Review and download a bounded disclosure packet from active case evidence."""

from dataclasses import asdict
from uuid import UUID

from django import forms
from django.core import signing
from django.db import transaction
from django.http import Http404, HttpResponse, QueryDict
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.crypto import constant_time_compare
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.evidence.collection import collection_context
from core.evidence.packet_output import PacketExhibit, packet_context, render_packet
from core.evidence.queries import scoped_evidence
from core.evidence.types import KIND_LABELS

MAX_EXHIBITS = 12
SELECTION_SALT = "pandect.evidence-packet.selection.v1"
REVIEW_SALT = "pandect.evidence-packet.review.v1"
ROLES = (
    ("source", "Source excerpt"),
    ("support", "Supporting material"),
    ("contrary", "Contrary material"),
    ("unresolved", "Unresolved material"),
)


class PacketForm(forms.Form):
    title = forms.CharField(label="Packet title", max_length=160)


class ExhibitForm(forms.Form):
    number = forms.IntegerField(label="Exhibit number", min_value=1)
    title = forms.CharField(label="Exhibit title", max_length=160)
    kind = forms.ChoiceField(label="Role in the packet", choices=ROLES)
    date = forms.DateField(
        label="Date for chronology",
        required=False,
        input_formats=["%Y-%m-%d"],
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        help_text="Leave blank if the date is unknown or disputed.",
    )
    citation = forms.CharField(
        label="Source citation",
        max_length=500,
        help_text=(
            "Identify the source and page, message, or recording time "
            "using only details you can disclose."
        ),
    )
    excerpt = forms.CharField(
        label="Text to disclose",
        max_length=6000,
        widget=forms.Textarea(attrs={"rows": 6}),
        help_text=(
            "Paste the passage or describe the item. "
            "Remove private material from this copy before previewing."
        ),
    )
    include_checksum = forms.BooleanField(
        label="Include the retained source checksum", required=False
    )

    def __init__(self, *args, source, **kwargs):
        super().__init__(*args, **kwargs)
        if not source["checksum"]:
            del self.fields["include_checksum"]

    def clean_excerpt(self):
        return self.cleaned_data["excerpt"].replace("\r\n", "\n").replace("\r", "\n")


def _source_ref(value):
    kind, separator, identifier = value.partition(":")
    if not separator or kind not in KIND_LABELS:
        raise ValueError
    return kind, UUID(identifier)


def _sources(case, refs):
    sources = []
    for ref in refs:
        kind, pk = _source_ref(ref)
        row = (
            scoped_evidence(case, kind)
            .filter(pk=pk, is_archived=False)
            .values("title", "sha256")
            .first()
        )
        if row is None:
            continue
        sources.append(
            {
                "ref": ref,
                "title": row["title"],
                "label": KIND_LABELS[kind],
                "checksum": row["sha256"],
                "url": reverse(
                    "core:evidence:detail",
                    kwargs={"case_id": case.pk, "kind": kind, "evidence_id": pk},
                ),
            }
        )
    return sources


def _signed_refs(case, actor, refs):
    return signing.dumps([str(case.pk), str(actor.pk), refs], salt=SELECTION_SALT)


def _refs(token, case, actor):
    if not token:
        return []
    if len(token) > 1800:
        raise signing.BadSignature
    case_id, actor_id, refs = signing.loads(token, salt=SELECTION_SALT)
    if (case_id, actor_id) != (str(case.pk), str(actor.pk)):
        raise Http404("Packet not found.")
    return refs


def _picker_url(case, token, params=None):
    query = QueryDict(mutable=True)
    for name in ("q", "type", "page"):
        if params and params.get(name):
            query[name] = params[name]
    if token:
        query["selection"] = token
    url = reverse("core:evidence:packet", args=[case.pk])
    return f"{url}?{query.urlencode()}" if query else url


@product_login_required
@require_http_methods(["GET", "POST"])
@never_cache
def packet_picker(request, case_id):
    case = get_case(request.user, case_id, edit=True)
    token = request.GET.get("selection", "")
    error = ""
    try:
        refs = _refs(token, case, request.user)
    except signing.BadSignature:
        error = "This selection could not be opened. Start a new packet."
        refs = []
    if request.method == "POST" and not error:
        remove = request.POST.get("remove")
        if remove in refs:
            refs = [ref for ref in refs if ref != remove]
        else:
            incoming = request.POST.getlist("source")
            try:
                if len(incoming) > MAX_EXHIBITS:
                    raise ValueError
                new_refs = [f"{kind}:{pk}" for kind, pk in map(_source_ref, incoming)]
                if len(_sources(case, new_refs)) != len(new_refs):
                    raise ValueError
                chosen = list(dict.fromkeys([*refs, *new_refs]))
                if len(chosen) > MAX_EXHIBITS:
                    error = f"Choose up to {MAX_EXHIBITS} exhibits."
                else:
                    refs = chosen
            except ValueError:
                error = "Choose active evidence from this case."
        if not error:
            return redirect(
                _picker_url(case, _signed_refs(case, request.user, refs), request.GET)
            )

    sources = _sources(case, refs)
    if len(sources) != len(refs):
        error = "Some selected evidence is no longer active or available."
        refs = [source["ref"] for source in sources]
        token = ""
    if not token or not refs:
        token = _signed_refs(case, request.user, refs) if refs else ""
    filters = request.GET.copy()
    allowed = {"q", "type", "page"}
    for key in set(filters) - allowed:
        del filters[key]
    context = collection_context(case, filters)
    for row in context["rows"]:
        row["ref"] = f"{row['kind']}:{row['id']}"
        row["selected"] = row["ref"] in refs
    for direction, href in context["pagination"].items():
        context["pagination"][direction] = _picker_url(
            case, token, QueryDict(href.removeprefix("?"))
        )
    return render(
        request,
        "core/evidence/packet_picker.html",
        case_context(case, request.user)
        | context
        | {
            "nav_section": "evidence",
            "selection": token,
            "selected_sources": sources,
            "limit": MAX_EXHIBITS,
            "error": error,
        },
    )


def _exhibits(packet_form, forms_with_sources):
    valid = [form.is_valid() for form, _ in forms_with_sources]
    if not packet_form.is_valid() or not all(valid):
        return []
    exhibits = {}
    for form, source in forms_with_sources:
        fields = form.cleaned_data
        number = fields["number"]
        if number > len(forms_with_sources):
            form.add_error("number", "Enter a number within the exhibit count.")
        elif number in exhibits:
            form.add_error("number", "Use each exhibit number once.")
        else:
            exhibits[number] = PacketExhibit(
                title=fields["title"],
                excerpt=fields["excerpt"],
                date=fields["date"],
                kind=dict(ROLES)[fields["kind"]],
                citation=fields["citation"],
                source_sha256=source["checksum"]
                if fields.get("include_checksum")
                else "",
            )
    if len(exhibits) != len(forms_with_sources):
        return []
    return [exhibits[n] for n in sorted(exhibits)]


def _review_content(token, title, exhibits):
    return [
        token,
        title,
        [
            asdict(exhibit)
            | {"date": exhibit.date.isoformat() if exhibit.date else None}
            for exhibit in exhibits
        ],
    ]


@product_login_required
@require_http_methods(["GET", "POST"])
@never_cache
def packet_review(request, case_id):
    data = request.POST if request.method == "POST" else None
    token = (request.POST if data is not None else request.GET).get("selection", "")
    action = data.get("action") if data is not None else None
    # Serialize the final access and source check with case changes and revocation.
    with transaction.atomic():
        case = get_case(request.user, case_id, edit=True, lock=action == "download")
        try:
            refs = _refs(token, case, request.user)
        except signing.BadSignature:
            refs = []
        sources = _sources(case, refs)
        if not sources or len(sources) != len(refs):
            return render(
                request,
                "core/evidence/packet_review.html",
                case_context(case, request.user)
                | {
                    "nav_section": "evidence",
                    "unavailable": True,
                    "picker_url": _picker_url(case, token),
                },
                status=409,
            )
        packet_form = PacketForm(data)
        forms_with_sources = [
            (
                ExhibitForm(
                    data,
                    prefix=f"exhibit-{number}",
                    source=source,
                    initial={"number": number, "kind": "source"},
                ),
                source,
            )
            for number, source in enumerate(sources, 1)
        ]
        context = case_context(case, request.user) | {
            "nav_section": "evidence",
            "selection": token,
            "picker_url": _picker_url(case, token),
            "form": packet_form,
            "forms_with_sources": forms_with_sources,
        }
        if action in {"preview", "download"} and (
            exhibits := _exhibits(packet_form, forms_with_sources)
        ):
            title = packet_form.cleaned_data["title"]
            content = _review_content(token, title, exhibits)
            # Bind the download to the exact disclosure rendered in the preview.
            signature = signing.Signer(salt=REVIEW_SALT).signature(
                signing.JSONSerializer().dumps(content)
            )
            if action == "download":
                if constant_time_compare(signature, data.get("review", "")):
                    response = HttpResponse(render_packet(title, exhibits))
                    response["Content-Disposition"] = (
                        'attachment; filename="evidence-packet.html"'
                    )
                    response["Content-Security-Policy"] = (
                        "default-src 'none'; style-src 'unsafe-inline'; sandbox"
                    )
                    response["X-Content-Type-Options"] = "nosniff"
                    return response
                context["error"] = (
                    "The packet changed. Review this version before downloading."
                )
            context["preview"] = packet_context(title, exhibits)
            context["review"] = signature
    return render(request, "core/evidence/packet_review.html", context)
