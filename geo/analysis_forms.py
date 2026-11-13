"""Domain-owned analysis assumptions and parameter validation."""

from datetime import UTC

from django import forms
from django.utils import timezone

from core.analysis.form_base import BaseAnalysisForm
from geo.analysis.deterministic.locations import LIMITS, comparison_periods


class GeoAnalysisForm(BaseAnalysisForm):
    context_radius_m = forms.FloatField(
        required=False,
        min_value=1,
        max_value=5000,
        initial=500,
        label="Map context search radius (metres)",
        help_text="The lookup area also includes the selected group members.",
    )

    context_as_of = forms.DateTimeField(
        required=False,
        label="Historical map snapshot (UTC, optional)",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local", "step": "1"}),
        help_text=(
            "Leave blank for the current map snapshot. A historical lookup requires "
            "the configured service to retain that date; current data is never "
            "substituted for an unavailable historical snapshot."
        ),
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

    operation_fields = {
        "normalize_addresses": (),
        "location_quality": ("distance_m",),
        "spatial_groups": ("distance_m",),
        "geographic_context": (
            "distance_m",
            "context_radius_m",
            "boundary_m",
            "context_as_of",
        ),
        "proximity": (
            "reference_latitude",
            "reference_longitude",
            "radius_m",
            "polygon",
            "boundary_m",
        ),
        "candidate_visits": ("distance_m", "gap_minutes", "dwell_minutes"),
        "candidate_trips": ("distance_m", "gap_minutes", "dwell_minutes"),
        "movement_routines": (
            "distance_m",
            "gap_minutes",
            "dwell_minutes",
            "period_a_start",
            "period_a_end",
            "period_b_start",
            "period_b_end",
        ),
        "colocation": ("distance_m", "time_window_minutes"),
        "travel_discrepancies": ("distance_m", "gap_minutes", "speed_limit_kmh"),
        "location_conflicts": ("event_label",),
        "geocode_locations": ("reverse",),
    }
    task_fields_by_operation = {
        "proximity": (
            "reference_latitude",
            "reference_longitude",
            "radius_m",
            "polygon",
        ),
        "location_conflicts": ("event_label",),
    }
    field_order = [
        "context_radius_m",
        "context_as_of",
        "period_a_start",
        "period_a_end",
        "period_b_start",
        "period_b_end",
        "boundary_m",
        "polygon",
        "backend",
        "operation",
        "submission_id",
        "sources",
        "external_processing",
        "distance_m",
        "gap_minutes",
        "dwell_minutes",
        "time_window_minutes",
        "speed_limit_kmh",
        "reference_latitude",
        "reference_longitude",
        "radius_m",
        "reverse",
        "event_label",
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.operation_name == "location_conflicts":
            self.fields["event_label"].required = True
        if self.operation_name in {"location_quality", "travel_discrepancies"}:
            self.fields[
                "distance_m"
            ].label = "Coordinate discrepancy threshold (metres)"
        if self.operation_name == "geographic_context":
            self.fields["boundary_m"] = forms.FloatField(
                required=False,
                min_value=0,
                max_value=100,
                initial=10,
                label="Boundary ambiguity distance (metres)",
            )

    def parameter_values(self):
        names = (
            "context_radius_m",
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
        )
        parameters = {
            key: self.cleaned_data[key]
            for key in names
            if self.cleaned_data.get(key) not in (None, "")
        }
        for key in (
            "context_as_of",
            "period_a_start",
            "period_a_end",
            "period_b_start",
            "period_b_end",
        ):
            if self.cleaned_data.get(key):
                parameters[key] = self.cleaned_data[key].isoformat()
        if self.cleaned_data.get("polygon"):
            parameters["polygon"] = self.cleaned_data["polygon"]
        return parameters

    def clean_polygon(self):
        value = self.cleaned_data["polygon"]
        if not value:
            return []
        try:
            polygon = [
                [float(x.strip()) for x in line.split(",")]
                for line in value.splitlines()
                if line.strip()
            ]
            if not 3 <= len(polygon) <= 100 or any(
                len(pair) != 2 or not -90 <= pair[0] <= 90 or not -180 <= pair[1] <= 180
                for pair in polygon
            ):
                raise ValueError
        except ValueError:
            raise forms.ValidationError(
                "Use 3 to 100 valid latitude, longitude pairs."
            ) from None
        return polygon

    def clean_context_as_of(self):
        value = self.cleaned_data["context_as_of"]
        if value is not None and value > timezone.now():
            raise forms.ValidationError("Choose a map snapshot no later than now.")
        if value is not None and value.microsecond:
            raise forms.ValidationError(
                "Use whole-second precision for the map snapshot."
            )
        return value.astimezone(UTC) if value is not None else None

    def clean(self):
        data = super().clean()
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
        return data
