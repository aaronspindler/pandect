"""Select an exact passage and record the investigator's reading of it."""

import re

from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

STATUSES = (
    ("unresolved", "Unresolved statement"),
    ("proposed", "Proposed appointment or plan"),
    ("cancelled", "Cancellation"),
    ("corrected", "Correction"),
    ("reported", "Reported event"),
)


def occurrence_choices(body, quote):
    """HTML form newlines can differ from immutable source bytes."""
    if not quote or len(quote) > 4000 or not quote.strip():
        return []
    text = body.replace("\r\n", "\n").replace("\r", "\n")
    needle = quote.replace("\r\n", "\n").replace("\r", "\n")
    matches = []
    cursor = 0
    while len(matches) <= 50:
        start = text.find(needle, cursor)
        if start < 0:
            break
        matches.append((start, start + len(needle)))
        cursor = start + 1

    # Browsers normalize CRLF to one character. Recover original offsets once
    # for the bounded match set, including overlapping occurrences.
    breaks = re.finditer("\r\n", body)
    following = next(breaks, None)
    offsets = {}
    skipped = 0
    for offset in sorted({position for pair in matches for position in pair}):
        while following is not None and following.start() < offset + skipped:
            skipped += 1
            following = next(breaks, None)
        offsets[offset] = offset + skipped
    positions = []
    for index, (left, right) in enumerate(matches, 1):
        start, end = offsets[left], offsets[right]
        caption = (
            body[max(0, start - 35) : start]
            + " › "
            + body[start : min(end, start + 50)]
            + " ‹ "
            + body[end : end + 35]
        )
        positions.append((str(start), end, f"Occurrence {index} · {caption}"))
    return positions


def span_for(form, field, occurrence, source):
    quote = form.cleaned_data.get(field)
    if not quote:
        return None
    positions = form.positions[occurrence]
    if not positions:
        form.add_error(field, "Copy the exact wording from the selected source.")
        return None
    if len(positions) > 50:
        form.add_error(
            field, "Include more surrounding words to distinguish this passage."
        )
        return None
    selected = form.cleaned_data.get(occurrence)
    if len(positions) == 1 and not selected:
        selected = positions[0][0]
    for start, end, _ in positions:
        if start == selected:
            return {"source_id": source.id, "start": int(start), "end": end}
    form.add_error(occurrence, "Choose the occurrence in its source context.")
    return None


def offset_time(value):
    if not value:
        return None
    try:
        instant = parse_datetime(value)
    except ValueError:
        instant = None
    if instant is None or timezone.is_naive(instant):
        raise ValidationError(
            "Enter a date and time with its UTC offset, such as 2026-11-21 14:00-05:00."
        )
    return instant.isoformat()


class InterpretationForm(forms.Form):
    quote = forms.CharField(
        label="Exact quotation",
        max_length=4000,
        strip=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    occurrence = forms.ChoiceField(
        label="Which occurrence?", required=False, widget=forms.RadioSelect
    )
    status = forms.ChoiceField(label="Statement type", choices=STATUSES)
    subject = forms.MultipleChoiceField(
        label="Claimed people or vehicles",
        required=False,
        help_text="Select the people or vehicles named together in this reading. "
        "Use another interpretation for alternative identities.",
    )
    address = forms.CharField(label="Place wording", max_length=500, required=False)
    place_resolved = forms.BooleanField(
        label="This interpretation identifies a specific address or coordinates",
        required=False,
    )
    latitude = forms.FloatField(required=False, min_value=-90, max_value=90)
    longitude = forms.FloatField(required=False, min_value=-180, max_value=180)
    radius_m = forms.IntegerField(
        label="Coordinate tolerance in metres",
        min_value=1,
        max_value=500000,
        initial=100,
        required=False,
    )
    time_basis = forms.ChoiceField(
        label="Time basis",
        choices=(
            ("unresolved", "Unresolved time"),
            ("explicit", "Explicit time in the source"),
            ("anchored_relative", "Relative time with a cited reference"),
        ),
    )
    time_start = forms.CharField(
        label="Start time",
        max_length=48,
        required=False,
        help_text="Include the UTC offset, for example 2026-11-21 14:00-05:00.",
    )
    time_end = forms.CharField(
        label="End time",
        max_length=48,
        required=False,
        help_text="Leave blank for a single instant.",
    )
    reference_source = forms.ChoiceField(label="Time reference source", required=False)
    reference_quote = forms.CharField(
        label="Exact time reference quotation",
        strip=False,
        max_length=4000,
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
    )
    reference_occurrence = forms.ChoiceField(
        label="Time reference occurrence", required=False, widget=forms.RadioSelect
    )
    applies_to = forms.ChoiceField(
        label="Statement cancelled or corrected", required=False
    )

    def __init__(self, data=None, *, subjects, option, references, claims):
        super().__init__(data)
        self.option = option
        self.references = references
        self.claims = claims
        self.fields["subject"].choices = subjects.items()
        self.fields["reference_source"].choices = [
            ("", "Choose a reference"),
            *((key, source.item.title) for key, source in references.items()),
        ]
        self.fields["applies_to"].choices = [
            ("", "No cited link"),
            *((str(i), row["quote"][:100]) for i, row in enumerate(claims)),
        ]
        if option["claim"]:
            self.fields["quote"].disabled = True
            self.initial.update(quote=option["quote"], status=option["claim"]["status"])
        self.positions = {}
        for name, source, quote_field in (
            ("occurrence", option["source"], "quote"),
            (
                "reference_occurrence",
                references.get(self.data.get("reference_source")),
                "reference_quote",
            ),
        ):
            positions = (
                occurrence_choices(source.text, self.data.get(quote_field, ""))
                if source
                and self.is_bound
                and not (name == "occurrence" and option["claim"])
                else []
            )
            self.positions[name] = positions
            self.fields[name].choices = (
                [(start, label) for start, _, label in positions]
                if 1 < len(positions) <= 50
                else []
            )
            if not self.fields[name].choices:
                self.fields[name].widget = forms.HiddenInput()
        if not claims:
            self.fields["applies_to"].widget = forms.HiddenInput()

    def clean_time_start(self):
        return offset_time(self.cleaned_data["time_start"])

    def clean_time_end(self):
        return offset_time(self.cleaned_data["time_end"])

    def clean(self):
        data = super().clean()
        claim = self.option["claim"]
        if claim:
            claim = dict(claim)
        else:
            claim = span_for(self, "quote", "occurrence", self.option["source"]) or {}
        if data.get("subject"):
            claim["subjects"] = sorted(data["subject"])
        for field in (
            "status",
            "address",
            "place_resolved",
            "latitude",
            "longitude",
            "radius_m",
            "time_basis",
            "time_start",
            "time_end",
        ):
            value = data.get(field)
            if value is not None and value != "":
                claim[field] = value
        if (data.get("latitude") is None) != (data.get("longitude") is None):
            self.add_error("latitude", "Supply both coordinates.")
        if data.get("time_basis") in ("explicit", "anchored_relative"):
            if not data.get("time_start"):
                self.add_error(
                    "time_start", "Supply the interpreted time and UTC offset."
                )
            elif not data.get("time_end"):
                claim["time_end"] = claim["time_start"]
        elif data.get("time_start") or data.get("time_end"):
            self.add_error(
                "time_basis",
                "Choose the basis for these times, or clear the time fields.",
            )
        if data.get("time_basis") == "anchored_relative":
            source = self.references.get(data.get("reference_source"))
            if not source:
                self.add_error(
                    "reference_source",
                    "Choose the source that anchors the relative time.",
                )
            elif not data.get("reference_quote"):
                self.add_error(
                    "reference_quote",
                    "Quote the wording that supplies the reference time.",
                )
            else:
                claim["reference"] = span_for(
                    self, "reference_quote", "reference_occurrence", source
                )
        elif data.get("reference_quote") or data.get("reference_source"):
            self.add_error(
                "time_basis", "Choose relative time to use a time reference."
            )
        if data.get("applies_to"):
            if data.get("status") not in ("cancelled", "corrected"):
                self.add_error(
                    "applies_to",
                    "Link the earlier statement only for a cancellation or correction.",
                )
            else:
                earlier = self.claims[int(data["applies_to"])]["claim"]
                claim["applies_to"] = {
                    key: earlier[key] for key in ("source_id", "start", "end")
                }
        data["claim"] = claim
        return data
