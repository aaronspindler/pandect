"""Shared analysis source selection and submission."""

from itertools import islice
from uuid import uuid4

from django import forms
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db.models import Exists, OuterRef, Q
from django.http import Http404
from django.utils import timezone

from core.analysis.manual import selected_analysis_variants
from core.analysis.operations import OPERATIONS, get_operation
from core.analysis.readiness import configuration_readiness, source_readiness
from core.evidence.queries import scoped_evidence
from core.evidence.types import CALL_KINDS, KIND_LABELS
from core.models import AnalysisResult, AnalysisReview
from phone.models import CallEvidenceAttachment
from ui.forms import localize_datetime_labels

SOURCE_PAGE_SIZE = 25
CALL_RECORDING_OPERATIONS = {"audio_metadata", "transcription"}
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


class SourceChoicePage(list):
    """A bounded cursor page; the heterogeneous choice stream has no total count."""

    def __init__(self, choices, *, number, previous, next_page, cursor=""):
        super().__init__(choices)
        self.number = number
        self.before = self[0][0] if self else cursor
        self.after = self[-1][0] if self else cursor
        self.previous = previous
        self.next = next_page

    def has_previous(self):
        return self.previous

    def has_next(self):
        return self.next

    def has_other_pages(self):
        return self.previous or self.next


def _after(queryset, item, *, descending=False, reverse=False, inclusive=False):
    """Seek the existing (created_at, UUID) order without scanning its prefix."""
    date_lookup = "lt" if descending != reverse else "gt"
    id_lookup = ("lt" if reverse else "gt") + ("e" if inclusive else "")
    return queryset.filter(
        Q(**{f"created_at__{date_lookup}": item.created_at})
        | Q(created_at=item.created_at, **{f"pk__{id_lookup}": item.pk})
    )


def _source_choices(
    case,
    operation,
    query="",
    *,
    evidence=None,
    cursor="",
    reverse=False,
    inclusive=False,
):
    """Seek originals, additions and exact extraction versions in either direction."""
    kinds = list(get_operation(operation).kinds)
    focused = parts = None
    if cursor:
        try:
            parts = cursor.split(":")
            if len(cursor) > 500 or not 2 <= len(parts) <= 5 or parts[0] not in kinds:
                raise ValueError
            focused = (
                scoped_evidence(case, parts[0])
                .only("id", "created_at")
                .get(pk=parts[1])
            )
            if evidence is not None and (
                parts[0] != evidence[0] or focused.pk != evidence[1].pk
            ):
                raise ValueError
        except ValueError, ValidationError, ObjectDoesNotExist:
            raise Http404("Source page not found.") from None
    cached_result = cached_candidates = None
    for kind in reversed(kinds) if reverse else kinds:
        if evidence is not None and kind != evidence[0]:
            continue
        if parts and (
            (kinds.index(kind) > kinds.index(parts[0]))
            if reverse
            else (kinds.index(kind) < kinds.index(parts[0]))
        ):
            continue
        queryset = scoped_evidence(case, kind).only(
            "id", "title", "is_archived", "created_at"
        )
        if evidence is not None:
            queryset = queryset.filter(pk=evidence[1].pk)
        if query:
            queryset = queryset.filter(title__icontains=query)
        if kind in CALL_KINDS and operation in CALL_RECORDING_OPERATIONS:
            parent = "phone_call" if kind == "phone-call" else "facetime"
            audio = CallEvidenceAttachment.objects.filter(
                case=case, **{f"{parent}_id": OuterRef("pk")}
            ).exclude(file="")
            queryset = queryset.filter(Exists(audio))
        if parts and kind == parts[0]:
            queryset = _after(
                queryset, focused, descending=True, reverse=reverse, inclusive=True
            )
        queryset = (
            queryset.order_by("created_at", "-pk")
            if reverse
            else queryset.order_by("-created_at", "pk")
        )
        for item in queryset.iterator(chunk_size=25):
            at_cursor = parts is not None and kind == parts[0] and item.pk == focused.pk
            cursor_phase = (
                (2 if len(parts) >= 4 else 1 if len(parts) == 3 else 0)
                if at_cursor
                else None
            )
            caption = f"{KIND_LABELS[kind]} · {item.title}" + (
                " · Archived" if item.is_archived else ""
            )
            for phase in (2, 1, 0) if reverse else (0, 1, 2):
                if cursor_phase is not None and (
                    (phase > cursor_phase) if reverse else (phase < cursor_phase)
                ):
                    continue
                seek = at_cursor and phase == cursor_phase
                if phase == 0:
                    if not (
                        kind in CALL_KINDS and operation in CALL_RECORDING_OPERATIONS
                    ) and (not seek or inclusive):
                        yield f"{kind}:{item.pk}", caption
                elif phase == 1 and kind in CALL_KINDS:
                    audio = operation in CALL_RECORDING_OPERATIONS
                    additions = (
                        item.attachments.filter(case=case)
                        .exclude(**{"file" if audio else "transcript": ""})
                        .only("id", "created_at", "original_filename")
                    )
                    if seek:
                        try:
                            addition = additions.get(pk=parts[2])
                        except ValueError, ValidationError, ObjectDoesNotExist:
                            raise Http404("Source page not found.") from None
                        additions = _after(
                            additions, addition, reverse=reverse, inclusive=inclusive
                        )
                    additions = (
                        additions.order_by("-created_at", "-pk")
                        if reverse
                        else additions.order_by("created_at", "pk")
                    )
                    for addition in additions.iterator(chunk_size=25):
                        stamp = timezone.localtime(addition.created_at)
                        label = (
                            addition.original_filename
                            if audio
                            else f"Supplied transcript {stamp:%d %b %Y %H:%M %Z}"
                        )
                        yield f"{kind}:{item.pk}:{addition.pk}", f"{caption} · {label}"
                elif (
                    phase == 2
                    and operation != "plate_ocr"
                    and operation not in CALL_RECORDING_OPERATIONS
                    and kind in EXTRACTIONS_BY_KIND
                ):
                    results = (
                        AnalysisResult.objects.filter(
                            case=case,
                            run__case=case,
                            run__status__in=("complete", "partial"),
                            run__operation__in=EXTRACTIONS_BY_KIND[kind],
                            run__inputs__contains=[
                                {"kind": kind, "evidence_id": str(item.pk)}
                            ],
                        )
                        .select_related("run")
                        .only(
                            "id",
                            "created_at",
                            "run__id",
                            "run__operation",
                            "run__inputs",
                        )
                    )
                    if EXTRACTIONS_BY_KIND[kind] == ("geocode_locations",):
                        newer = AnalysisReview.objects.filter(
                            case=case,
                            result_id=OuterRef("result_id"),
                            item_index=OuterRef("item_index"),
                        ).filter(
                            Q(created_at__gt=OuterRef("created_at"))
                            | Q(
                                created_at=OuterRef("created_at"), pk__lt=OuterRef("pk")
                            )
                        )
                        accepted = AnalysisReview.objects.filter(
                            case=case,
                            result_id=OuterRef("pk"),
                            item_index__isnull=False,
                            decision="accepted",
                        ).filter(~Exists(newer))
                        results = results.filter(Exists(accepted))
                    cursor_result = None
                    if seek:
                        try:
                            cursor_result = results.get(pk=parts[3])
                        except ValueError, ValidationError, ObjectDoesNotExist:
                            raise Http404("Source page not found.") from None
                        results = _after(
                            results,
                            cursor_result,
                            descending=True,
                            reverse=reverse,
                            inclusive=True,
                        )
                    results = (
                        results.order_by("created_at", "-pk")
                        if reverse
                        else results.order_by("-created_at", "pk")
                    )
                    for result in results.iterator(chunk_size=1):
                        choices = []
                        candidates = None
                        for selected in result.run.inputs:
                            if selected.get("kind") != kind or selected.get(
                                "evidence_id"
                            ) != str(item.pk):
                                continue
                            attachment = selected.get("attachment_id") or "-"
                            prefix = f"{kind}:{item.pk}:{attachment}:{result.pk}"
                            method = get_operation(result.run.operation).label
                            stamp = timezone.localtime(result.created_at)
                            label = f"{caption} · {method} · {stamp:%d %b %Y %H:%M %Z}"
                            if result.run.operation == "geocode_locations":
                                if candidates is None and result.pk == cached_result:
                                    candidates = cached_candidates
                                if candidates is None:
                                    latest = dict(
                                        result.reviews.filter(case=case)
                                        .order_by("item_index", "-created_at", "id")
                                        .distinct("item_index")
                                        .values_list("item_index", "decision")
                                    )
                                    candidates = [
                                        (index, candidate)
                                        for index, candidate in enumerate(
                                            result.data.get("items", [])
                                        )
                                        if latest.get(index) == "accepted"
                                        and candidate.get("values", {}).get("latitude")
                                        is not None
                                    ]
                                    cached_result, cached_candidates = (
                                        result.pk,
                                        candidates,
                                    )
                                for index, candidate in candidates:
                                    if selected["id"] in candidate.get(
                                        "source_ids", []
                                    ):
                                        address = candidate["values"].get("address", "")
                                        choices.append(
                                            (
                                                f"{prefix}:{index}",
                                                f"{label} · Address estimate · "
                                                f"{candidate['label']} · "
                                                f"{address}",
                                            )
                                        )
                            else:
                                choices.append((prefix, label))
                        if cursor_result is not None and result.pk == cursor_result.pk:
                            position = next(
                                (
                                    index
                                    for index, choice in enumerate(choices)
                                    if choice[0] == cursor
                                ),
                                None,
                            )
                            if position is None:
                                raise Http404("Source page not found.")
                            choices = (
                                choices[: position + int(inclusive)]
                                if reverse
                                else choices[position + int(not inclusive) :]
                            )
                        yield from reversed(choices) if reverse else choices


def _source_choice_page(
    case,
    operation,
    query="",
    *,
    evidence=None,
    page=1,
    selected=(),
    cursor="",
    reverse=False,
):
    choices = list(
        islice(
            _source_choices(
                case,
                operation,
                query,
                evidence=evidence,
                cursor=cursor,
                reverse=reverse,
            ),
            SOURCE_PAGE_SIZE + 1,
        )
    )
    has_more = len(choices) > SOURCE_PAGE_SIZE
    choices = choices[:SOURCE_PAGE_SIZE]
    if reverse:
        choices.reverse()
    try:
        number = max(1, int(page))
    except TypeError, ValueError:
        number = 1
    page_obj = SourceChoicePage(
        choices,
        number=number,
        previous=has_more if reverse else bool(cursor),
        next_page=bool(cursor) if reverse else has_more,
        cursor=cursor,
    )
    retained = []
    current = dict(choices)
    for value in dict.fromkeys(selected):
        if value in current:
            retained.append((value, current[value]))
            continue
        try:
            choice = next(
                _source_choices(
                    case, operation, evidence=evidence, cursor=value, inclusive=True
                ),
                None,
            )
        except Http404:
            continue
        if choice is not None and choice[0] == value:
            retained.append(choice)
    return page_obj, retained


def source_choices(
    case, operation, query="", *, evidence=None, page=1, cursor="", reverse=False
):
    return _source_choice_page(
        case,
        operation,
        query,
        evidence=evidence,
        page=page,
        cursor=cursor,
        reverse=reverse,
    )[0]


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
    operation = forms.ChoiceField(
        choices=[(key, value.label) for key, value in OPERATIONS.items()],
        widget=forms.HiddenInput,
    )

    submission_id = forms.UUIDField(initial=uuid4, widget=forms.HiddenInput)

    sources = forms.MultipleChoiceField(
        label="Retained sources", widget=forms.CheckboxSelectMultiple
    )

    operation_fields = {}
    task_fields_by_operation = {}

    def __init__(
        self,
        *args,
        case,
        operation,
        query="",
        evidence=None,
        sources_page=1,
        sources_cursor="",
        sources_reverse=False,
        selected_sources=(),
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.operation_name = operation
        self.parameter_names = self.operation_fields.get(operation, ())
        self.task_field_names = self.task_fields_by_operation.get(operation, ())
        self.assumption_names = tuple(
            name for name in self.parameter_names if name not in self.task_field_names
        )
        active_fields = {"operation", "submission_id", "sources", *self.parameter_names}
        self.fields = {
            name: field for name, field in self.fields.items() if name in active_fields
        }
        self.processing_unavailable = not any(
            selected_analysis_variants(case, operation, {})
        )
        self.configuration_readiness = (
            configuration_readiness(operation, case=case)
            if self.processing_unavailable
            else ""
        )
        self.fields["operation"].initial = operation
        selected = (
            self.data.getlist("sources")
            if self.is_bound and hasattr(self.data, "getlist")
            else self.data.get("sources", [])
            if self.is_bound
            else selected_sources
        )
        self.sources_cursor = sources_cursor
        self.sources_reverse = sources_reverse
        self.source_page, retained = _source_choice_page(
            case,
            operation,
            query,
            evidence=evidence,
            page=sources_page,
            cursor=sources_cursor,
            reverse=sources_reverse,
            selected=selected[: settings.PANDECT_ANALYSIS_MAX_SOURCES],
        )
        current = dict(self.source_page)
        self.retained_sources = [
            (value, label) for value, label in retained if value not in current
        ]
        choices = [*self.source_page, *self.retained_sources]
        self.fields["sources"].choices = choices
        self.readiness = source_readiness(case, operation, choices)
        if evidence is not None and choices:
            self.initial["sources"] = [choices[0][0]]
        localize_datetime_labels(self)

    @property
    def assumptions(self):
        return [self[name] for name in self.assumption_names]

    @property
    def task_fields(self):
        return [self[name] for name in self.task_field_names]

    def parameter_values(self):
        return {}

    def parameters(self):
        return self.parameter_values()

    def clean(self):
        data = super().clean()
        if len(data.get("sources", [])) > settings.PANDECT_ANALYSIS_MAX_SOURCES:
            self.add_error(
                "sources",
                f"Choose at most {settings.PANDECT_ANALYSIS_MAX_SOURCES} "
                "sources for one analysis.",
            )
        blocked = [
            self.readiness[value]["label"]
            for value in data.get("sources", [])
            if self.readiness.get(value, {}).get("blocking")
        ]
        if blocked:
            self.add_error(
                "sources",
                "Resolve the selected sources before analysis: "
                + ", ".join(dict.fromkeys(blocked))
                + ". No sources were dropped from your selection.",
            )
        return data
