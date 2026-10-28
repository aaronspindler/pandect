"""Investigator-facing bounded selections and visible analysis assumptions."""

from uuid import uuid4

from django import forms

from evidence.queries import scoped_evidence
from evidence.types import CALL_KINDS, KIND_LABELS
from people.models import Person

from .locations import LIMITS, comparison_periods
from .models import AnalysisResult
from .operations import OPERATIONS, get_operation
from .sources import source_id

SOURCE_CHOICE_LIMIT = 500


class ReportForm(forms.Form):
    state = forms.ChoiceField(
        choices=(
            ("active", "Active"),
            ("archived", "Archived"),
            ("all", "All evidence"),
        ),
        initial="active",
    )
    start = forms.DateTimeField(
        required=False,
        label="From (UTC)",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )
    end = forms.DateTimeField(
        required=False,
        label="Through (UTC)",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )
    vehicle_id = forms.ChoiceField(required=False, label="Vehicle")
    participant_id = forms.ChoiceField(required=False, label="Participant")
    other_participant_id = forms.ChoiceField(
        required=False, label="Other participant in the pair"
    )

    def __init__(self, *args, case, report_name=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["vehicle_id"].choices = [
            ("", "All vehicles"),
            *((str(x.pk), str(x)) for x in case.vehicles.all()),
        ]
        self.fields["participant_id"].choices = [
            ("", "All participants"),
            *((str(x.pk), str(x)) for x in Person.objects.filter(case=case)),
        ]
        self.fields["other_participant_id"].choices = self.fields[
            "participant_id"
        ].choices
        if report_name == "calls":
            del self.fields["vehicle_id"]
        elif report_name is not None:
            del self.fields["participant_id"]
            del self.fields["other_participant_id"]
            if report_name == "duplicates":
                del self.fields["vehicle_id"]

    def clean(self):
        data = super().clean()
        if data.get("other_participant_id") and not data.get("participant_id"):
            self.add_error("participant_id", "Choose both participants for a pair.")
        if data.get("start") and data.get("end") and data["start"] > data["end"]:
            self.add_error("end", "The end must follow the start.")
        return data


def source_choices(case, operation, query=""):
    """Bounded source picker; every submitted ID is resolved again by the service."""
    choices = []
    specification = get_operation(operation)
    for kind in specification.kinds:
        queryset = (
            scoped_evidence(case, kind)
            .only("id", "title", "is_archived", "created_at")
            .order_by("-created_at", "pk")
        )
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
            # Exact completed extraction versions are explicit source choices.
            results = (
                AnalysisResult.objects.filter(
                    case=case,
                    run__case=case,
                    run__status__in=("complete", "partial"),
                    run__operation__in=(
                        "native_text",
                        "ocr",
                        "transcription",
                        "geocode_locations",
                    ),
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
                        candidates = [
                            (index, x)
                            for index, x in enumerate(result.data.get("items", []))
                            if selected["id"] in x.get("source_ids", [])
                            and x.get("values", {}).get("latitude") is not None
                        ]
                        for index, candidate in candidates:
                            review = result.reviews.filter(item_index=index).first()
                            if review and review.decision == "accepted":
                                choices.append(
                                    (
                                        f"{prefix}:{index}",
                                        f"{label} · {candidate['label']} · "
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


class AnalysisForm(forms.Form):
    compare_split_at = forms.DateTimeField(
        required=False,
        label="Compare communications before and after (UTC)",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )
    period_a_start = forms.DateTimeField(
        required=False,
        label="First movement period from (UTC)",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )
    period_a_end = forms.DateTimeField(
        required=False,
        label="First movement period until (UTC, exclusive)",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )
    period_b_start = forms.DateTimeField(
        required=False,
        label="Second movement period from (UTC)",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )
    period_b_end = forms.DateTimeField(
        required=False,
        label="Second movement period until (UTC, exclusive)",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )
    boundary_m = forms.FloatField(
        required=False,
        min_value=LIMITS["boundary_m"][1],
        max_value=LIMITS["boundary_m"][2],
        initial=LIMITS["boundary_m"][0],
        label="Boundary ambiguity distance (metres)",
    )
    me_label = forms.CharField(
        required=False, max_length=255, initial="Me", label="First source account label"
    )
    them_label = forms.CharField(
        required=False,
        max_length=255,
        initial="Them",
        label="Second source account label",
    )
    sample_origin = forms.ChoiceField(
        required=False,
        choices=(
            ("original", "Original writing"),
            ("translated", "Translated"),
            ("edited", "Edited"),
            ("dictated", "Dictated"),
            ("ocr", "OCR"),
            ("transcription", "Transcribed"),
        ),
        initial="original",
        label="Writing sample context",
    )
    sample_language = forms.CharField(
        required=False,
        max_length=100,
        label="Reported language or languages",
        help_text="Source context, not automatic language identification.",
    )
    sample_channel = forms.CharField(
        required=False, max_length=100, label="Writing channel"
    )
    sample_topic = forms.CharField(
        required=False, max_length=200, label="Writing topic"
    )
    comparison_channel = forms.CharField(
        required=False, max_length=100, label="Second sample channel"
    )
    comparison_topic = forms.CharField(
        required=False, max_length=200, label="Second sample topic"
    )
    polygon = forms.CharField(
        required=False,
        max_length=8000,
        widget=forms.Textarea,
        label="Optional area boundary",
        help_text=(
            "One latitude, longitude pair per line; 3 to 100 vertices. "
            "A boundary replaces the radius query."
        ),
    )
    group_a = forms.MultipleChoiceField(
        required=False,
        label="First writing sample",
        widget=forms.SelectMultiple(attrs={"size": 8}),
    )
    group_b = forms.MultipleChoiceField(
        required=False,
        label="Second writing sample",
        widget=forms.SelectMultiple(attrs={"size": 8}),
    )
    break_before = forms.MultipleChoiceField(
        required=False,
        label="Start an inferred session before",
        widget=forms.SelectMultiple(attrs={"size": 6}),
    )
    join_before = forms.MultipleChoiceField(
        required=False,
        label="Continue an inferred session before",
        widget=forms.SelectMultiple(attrs={"size": 6}),
    )
    exclude_phrases = forms.CharField(
        required=False,
        max_length=8000,
        widget=forms.Textarea,
        label="Quoted or copied wording to exclude",
        help_text=(
            "One exact phrase per line. All matching occurrences in the "
            "selected original text are excluded from writing measurements."
        ),
    )
    include_summary = forms.BooleanField(
        required=False,
        label="Include a source-cited topic summary",
        help_text="Requires approval for external AI processing.",
    )
    semantic_links = forms.BooleanField(
        required=False,
        label="Suggest topic continuation across channels",
        help_text="Requires approval for external AI processing.",
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
            "AI operations send selected content through the operator's "
            "Cloudflare AI Gateway. Geographic lookup sends selected "
            "addresses or coordinates to the configured lookup service. "
            "Previously sent requests cannot be recalled. Operator "
            "retention policies apply."
        ),
    )
    distance_m = forms.FloatField(
        required=False,
        min_value=1,
        max_value=100000,
        label="Place grouping distance (metres)",
        initial=100,
    )
    gap_minutes = forms.FloatField(
        required=False,
        min_value=1,
        max_value=10080,
        label="Observation/session gap (minutes)",
        initial=60,
    )
    dwell_minutes = forms.FloatField(
        required=False,
        min_value=0,
        max_value=LIMITS["dwell_minutes"][2],
        label="Minimum observed stop (minutes)",
        initial=5,
    )
    time_window_minutes = forms.FloatField(
        required=False,
        min_value=0,
        max_value=1440,
        label="Comparison time window (minutes)",
        initial=10,
    )
    speed_limit_kmh = forms.FloatField(
        required=False,
        min_value=1,
        max_value=LIMITS["speed_limit_kmh"][2],
        label="Transition review threshold (km/h)",
        initial=300,
        help_text="A review threshold, not an applicable road speed limit.",
    )
    reference_latitude = forms.FloatField(
        required=False, min_value=-90, max_value=90, label="Area centre latitude"
    )
    reference_longitude = forms.FloatField(
        required=False, min_value=-180, max_value=180, label="Area centre longitude"
    )
    radius_m = forms.FloatField(
        required=False,
        min_value=1,
        max_value=LIMITS["radius_m"][2],
        label="Area radius (metres)",
        initial=100,
    )
    reverse = forms.BooleanField(
        required=False, label="Find addresses from coordinates"
    )
    event_label = forms.CharField(
        required=False, max_length=200, label="Claimed event being compared"
    )
    timezone = forms.CharField(
        required=False,
        initial="UTC",
        label="Export timezone",
        help_text="An explicit IANA timezone, such as America/Toronto.",
    )
    me = forms.ModelChoiceField(
        queryset=Person.objects.none(),
        required=False,
        label="Export account labelled Me",
    )
    them = forms.ModelChoiceField(
        queryset=Person.objects.none(),
        required=False,
        label="Export account labelled Them",
    )

    def __init__(self, *args, case, operation, query="", **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["operation"].initial = operation
        self.fields["sources"].choices = source_choices(case, operation, query)
        for name in ("group_a", "group_b", "break_before", "join_before"):
            self.fields[name].choices = self.fields["sources"].choices
        people = Person.objects.filter(case=case)
        self.fields["me"].queryset = people
        self.fields["them"].queryset = people
        assumption_names = []
        location_fields = {
            "normalize_addresses": [],
            "location_quality": ["gap_minutes"],
            "spatial_groups": ["distance_m"],
            "proximity": [
                "reference_latitude",
                "reference_longitude",
                "radius_m",
                "polygon",
                "boundary_m",
            ],
            "candidate_visits": ["distance_m", "gap_minutes", "dwell_minutes"],
            "candidate_trips": ["distance_m", "gap_minutes", "dwell_minutes"],
            "movement_routines": [
                "distance_m",
                "gap_minutes",
                "dwell_minutes",
                "period_a_start",
                "period_a_end",
                "period_b_start",
                "period_b_end",
            ],
            "colocation": ["distance_m", "time_window_minutes"],
            "travel_discrepancies": ["gap_minutes", "speed_limit_kmh"],
            "location_conflicts": ["distance_m", "time_window_minutes", "event_label"],
        }
        assumption_names = location_fields.get(operation, [])
        if operation == "geocode_locations":
            assumption_names = ["reverse"]
        if operation in {"sessions", "session_summaries", "comm_patterns"}:
            assumption_names = ["gap_minutes", "break_before", "join_before"]
            if operation == "comm_patterns":
                assumption_names.append("compare_split_at")
        if operation == "style_change":
            assumption_names = [
                "group_a",
                "group_b",
                "exclude_phrases",
                "sample_origin",
                "sample_language",
                "sample_channel",
                "sample_topic",
                "comparison_channel",
                "comparison_topic",
            ]
        if operation == "style_profiles":
            assumption_names = [
                "exclude_phrases",
                "sample_origin",
                "sample_language",
                "sample_channel",
                "sample_topic",
            ]
        if operation == "text_topics":
            assumption_names = ["include_summary"]
        if operation == "conversation_link":
            assumption_names = ["time_window_minutes", "semantic_links"]
        self.assumptions = [self[name] for name in assumption_names]

    def parameters(self):
        names = (
            "distance_m",
            "boundary_m",
            "gap_minutes",
            "dwell_minutes",
            "time_window_minutes",
            "speed_limit_kmh",
            "reference_latitude",
            "reference_longitude",
            "radius_m",
            "reverse",
            "event_label",
            "timezone",
            "include_summary",
            "semantic_links",
            "exclude_phrases",
        )
        parameters = {
            key: self.cleaned_data[key]
            for key in names
            if self.cleaned_data.get(key) not in (None, "")
        }
        for key in (
            "compare_split_at",
            "period_a_start",
            "period_a_end",
            "period_b_start",
            "period_b_end",
        ):
            if self.cleaned_data.get(key):
                parameters[key] = self.cleaned_data[key].isoformat()
        if self.cleaned_data.get("me") and self.cleaned_data.get("them"):
            me, them = (
                str(self.cleaned_data["me"].pk),
                str(self.cleaned_data["them"].pk),
            )
            first = self.cleaned_data.get("me_label") or "Me"
            second = self.cleaned_data.get("them_label") or "Them"
            parameters |= {
                "sender_map": {first: me, second: them},
                "recipient_map": {first: them, second: me},
            }
        for key in ("group_a", "group_b", "break_before", "join_before"):
            if self.cleaned_data.get(key):
                parameters[key] = [
                    source_id(selection(x)) for x in self.cleaned_data[key]
                ]
        if self.cleaned_data.get("polygon"):
            parameters["polygon"] = [
                [float(x.strip()) for x in line.split(",")]
                for line in self.cleaned_data["polygon"].splitlines()
                if line.strip()
            ]
        if self.cleaned_data.get("operation") in {"style_profiles", "style_change"}:
            contexts = {}
            for choice in self.cleaned_data.get("sources", []):
                key = source_id(selection(choice))
                second = key in parameters.get("group_b", [])
                contexts[key] = {
                    "origin": self.cleaned_data.get("sample_origin") or "original",
                    "language": self.cleaned_data.get("sample_language", ""),
                    "channel": self.cleaned_data.get(
                        "comparison_channel" if second else "sample_channel", ""
                    ),
                    "topic": self.cleaned_data.get(
                        "comparison_topic" if second else "sample_topic", ""
                    ),
                }
            parameters["contexts"] = contexts
        return parameters

    def clean(self):
        data = super().clean()
        chosen = set(data.get("sources", []))
        try:
            comparison_periods(
                {
                    key: data[key].isoformat()
                    for key in (
                        "period_a_start",
                        "period_a_end",
                        "period_b_start",
                        "period_b_end",
                    )
                    if data.get(key)
                }
            )
        except ValueError:
            self.add_error(
                "period_a_start",
                "Choose two complete, ordered, non-overlapping movement periods.",
            )
        if data.get("me_label") and data.get("me_label") == data.get("them_label"):
            self.add_error("them_label", "Use different source account labels.")
        if (
            data.get("operation") == "proximity"
            and not data.get("polygon")
            and (
                data.get("reference_latitude") is None
                or data.get("reference_longitude") is None
            )
        ):
            self.add_error(
                "reference_latitude",
                "Choose both centre coordinates or an area boundary.",
            )
        if data.get("polygon"):
            try:
                polygon = [
                    [float(x.strip()) for x in line.split(",")]
                    for line in data["polygon"].splitlines()
                    if line.strip()
                ]
                if not 3 <= len(polygon) <= 100 or any(
                    len(pair) != 2
                    or not -90 <= pair[0] <= 90
                    or not -180 <= pair[1] <= 180
                    for pair in polygon
                ):
                    raise ValueError
            except ValueError:
                self.add_error(
                    "polygon", "Use 3 to 100 valid latitude, longitude pairs."
                )
        for key in ("group_a", "group_b", "break_before", "join_before"):
            if set(data.get(key, [])) - chosen:
                self.add_error(key, "Choose sources included in this analysis.")
        if data.get("operation") == "style_change" and (
            not data.get("group_a")
            or not data.get("group_b")
            or set(data.get("group_a", [])) & set(data.get("group_b", []))
        ):
            self.add_error("group_b", "Choose two nonempty, separate writing samples.")
        if (
            data.get("include_summary")
            or data.get("semantic_links")
            or get_operation(data.get("operation", "native_text")).external
        ) and not data.get("external_processing"):
            self.add_error(
                "external_processing",
                "Approve external processing for the selected operation.",
            )
        return data
