"""Shared analysis source selection, submission and processing approval."""

from uuid import uuid4

from django import forms

from core.analysis.execution import processing_external, requires_inference
from core.analysis.operations import OPERATIONS, get_operation
from core.evidence.queries import scoped_evidence
from core.evidence.types import CALL_KINDS, KIND_LABELS
from core.models import AnalysisResult

SOURCE_CHOICE_LIMIT = 500
EXTRACTIONS_BY_KIND = {
    "document": ("native_text", "ocr"),
    "image": ("ocr", "plate_ocr"),
    "audio": ("transcription",),
    "phone-call": ("transcription",),
    "facetime": ("transcription",),
    "location": ("geocode_locations",),
    "person-location": ("geocode_locations",),
    "vehicle-location": ("geocode_locations",),
}


def source_choices(case, operation, query="", *, evidence=None):
    """Bounded source picker; every submitted ID is resolved again by the service."""
    choices = []
    geocode_candidates = {}
    specification = get_operation(operation)
    for kind in specification.kinds:
        if evidence is not None and kind != evidence[0]:
            continue
        queryset = (
            scoped_evidence(case, kind)
            .only("id", "title", "is_archived", "created_at")
            .order_by("-created_at", "pk")
        )
        if evidence is not None:
            queryset = queryset.filter(pk=evidence[1].pk)
        if query:
            queryset = queryset.filter(title__icontains=query)
        for item in queryset[:SOURCE_CHOICE_LIMIT].iterator(chunk_size=50):
            caption = f"{KIND_LABELS[kind]} · {item.title}" + (
                " · Archived" if item.is_archived else ""
            )
            if kind in CALL_KINDS and operation in {"audio_metadata", "transcription"}:
                additions = (
                    item.attachments.filter(case=case)
                    .exclude(file="")
                    .only("id", "original_filename")
                )
                choices.extend(
                    (f"{kind}:{item.pk}:{x.pk}", f"{caption} · {x.original_filename}")
                    for x in additions[: min(50, SOURCE_CHOICE_LIMIT - len(choices))]
                )
            else:
                choices.append((f"{kind}:{item.pk}", caption))
                if len(choices) >= SOURCE_CHOICE_LIMIT:
                    return choices
                if kind in CALL_KINDS:
                    additions = (
                        item.attachments.filter(case=case)
                        .only("id", "created_at")
                        .exclude(transcript="")
                    )
                    choices.extend(
                        (
                            f"{kind}:{item.pk}:{x.pk}",
                            f"{caption} · Supplied transcript "
                            f"{x.created_at:%d %b %Y %H:%M} UTC",
                        )
                        for x in additions[
                            : min(50, SOURCE_CHOICE_LIMIT - len(choices))
                        ]
                    )
            if len(choices) >= SOURCE_CHOICE_LIMIT:
                return choices
            if operation == "plate_ocr":
                continue
            extractions = EXTRACTIONS_BY_KIND.get(kind)
            if not extractions:
                continue
            # Exact completed extraction versions are explicit source choices.
            results = (
                AnalysisResult.objects.filter(
                    case=case,
                    run__case=case,
                    run__status__in=("complete", "partial"),
                    run__operation__in=extractions,
                    run__inputs__contains=[{"kind": kind, "evidence_id": str(item.pk)}],
                )
                .select_related("run")
                .only("id", "created_at", "run__id", "run__operation", "run__inputs")
                .order_by("-created_at")[:10]
            )
            for result in results.iterator(chunk_size=1):
                for selected in result.run.inputs:
                    if selected.get("kind") != kind or selected.get(
                        "evidence_id"
                    ) != str(item.pk):
                        continue
                    attachment = selected.get("attachment_id") or "-"
                    prefix = f"{kind}:{item.pk}:{attachment}:{result.pk}"
                    label = (
                        f"{caption} · {get_operation(result.run.operation).label} · "
                        f"{result.created_at:%d %b %Y %H:%M} UTC"
                    )
                    if result.run.operation == "geocode_locations":
                        if result.pk not in geocode_candidates:
                            # A multi-source lookup appears under several originals.
                            # Load its candidate data and latest reviews only once.
                            latest = dict(
                                result.reviews.filter(case=case)
                                .order_by("item_index", "-created_at", "id")
                                .distinct("item_index")
                                .values_list("item_index", "decision")
                            )
                            geocode_candidates[result.pk] = [
                                (index, candidate)
                                for index, candidate in enumerate(
                                    result.data.get("items", [])
                                )
                                if latest.get(index) == "accepted"
                                and candidate.get("values", {}).get("latitude")
                                is not None
                            ]
                        candidates = [
                            (index, x)
                            for index, x in geocode_candidates[result.pk]
                            if selected["id"] in x.get("source_ids", [])
                        ]
                        for index, candidate in candidates:
                            choices.append(
                                (
                                    f"{prefix}:{index}",
                                    f"{label} · Reviewed estimate · "
                                    f"{candidate['label']} · "
                                    f"{candidate['values'].get('address', '')}",
                                )
                            )
                            if len(choices) >= SOURCE_CHOICE_LIMIT:
                                return choices
                    else:
                        choices.append((prefix, label))
                        if len(choices) >= SOURCE_CHOICE_LIMIT:
                            return choices
    return choices


def selection(value):
    parts = value.split(":")
    data = {"kind": parts[0], "id": parts[1]}
    if len(parts) > 2 and parts[2] != "-":
        data["attachment_id"] = parts[2]
    if len(parts) > 3:
        data["result_id"] = parts[3]
    if len(parts) > 4:
        data["geocode_candidate"] = int(parts[4])
    return data


class BaseAnalysisForm(forms.Form):
    backend = forms.ChoiceField(
        required=False,
        initial="local",
        label="Processing location",
        choices=(
            ("local", "This server or private network"),
            ("gateway", "External AI service"),
        ),
        help_text=(
            "Local processing keeps evidence on the operator's own hardware. "
            "An unavailable local service never falls back to external processing."
        ),
    )

    operation = forms.ChoiceField(
        choices=[(key, value.label) for key, value in OPERATIONS.items()],
        widget=forms.HiddenInput,
    )

    submission_id = forms.UUIDField(initial=uuid4, widget=forms.HiddenInput)

    sources = forms.MultipleChoiceField(
        label="Retained sources", widget=forms.CheckboxSelectMultiple
    )

    external_processing = forms.BooleanField(
        required=False,
        label="I approve external processing of the selected evidence",
        help_text=(
            "Required only when using an external service. External AI sends "
            "selected content through the operator's "
            "Cloudflare AI Gateway. Geographic lookup sends selected "
            "addresses or coordinates to the configured lookup service. "
            "Previously sent requests cannot be recalled. Operator "
            "retention policies apply."
        ),
    )

    operation_fields = {}

    def __init__(self, *args, case, operation, query="", evidence=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.operation_name = operation
        self.parameter_names = self.operation_fields.get(operation, ())
        self.assumption_names = self.parameter_names
        uses_model = requires_inference(
            operation, {"include_summary": True, "semantic_links": True}
        )
        active_fields = {
            "operation",
            "submission_id",
            "sources",
            "external_processing",
            *self.parameter_names,
        }
        if uses_model:
            active_fields.add("backend")
        self.fields = {
            name: field for name, field in self.fields.items() if name in active_fields
        }
        if not uses_model and not processing_external(operation, {}):
            self.fields["external_processing"].widget = forms.HiddenInput()
            self.fields["external_processing"].disabled = True
        self.fields["operation"].initial = operation
        choices = source_choices(case, operation, query, evidence=evidence)
        self.fields["sources"].choices = choices
        if evidence is not None and choices:
            self.initial["sources"] = [choices[0][0]]

    @property
    def assumptions(self):
        return [self[name] for name in self.assumption_names]

    def parameter_values(self):
        return {}

    def parameters(self):
        parameters = self.parameter_values()
        if requires_inference(self.cleaned_data.get("operation"), parameters):
            parameters["backend"] = self.cleaned_data.get("backend") or "local"
        return parameters

    def clean(self):
        data = super().clean()
        external = processing_external(
            data.get("operation", "native_text"),
            data,
            backend=data.get("backend") or "local",
        )
        if external and not data.get("external_processing"):
            self.add_error(
                "external_processing",
                "Approve external processing for the selected operation.",
            )
        if not external:
            data["external_processing"] = False
        return data
