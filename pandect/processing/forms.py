"""Operator decision forms for durable processing evidence."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django import forms

from pandect.identities.models import IdentityCandidate, Subject
from pandect.processing.extractors.timestamps import parse_timestamp_value


class TimestampPromotionForm(forms.Form):
    """Resolve one timestamp claim without guessing missing timezone evidence."""

    timezone_name = forms.CharField(
        required=False,
        max_length=64,
        label="Timezone",
        help_text=(
            "If the timestamp has no UTC offset, enter an IANA timezone such as "
            "America/Toronto. Use evidence from the source to choose it."
        ),
    )
    fold = forms.TypedChoiceField(
        required=False,
        coerce=int,
        empty_value=None,
        label="Repeated local time",
        choices=(
            ("", "Choose when required"),
            ("0", "First occurrence (before the clock moves back)"),
            ("1", "Second occurrence (after the clock moves back)"),
        ),
        help_text=(
            "Choose only if the clock moved back and this local time occurred "
            "twice. Otherwise leave this blank."
        ),
    )
    operator_note = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        label="Decision note",
        help_text="Optional. Record why you chose this timestamp.",
    )

    def __init__(self, *args, claim, **kwargs):
        super().__init__(*args, **kwargs)
        self.claim = claim
        self.resulting_occurred_at: datetime | None = None
        self.resolved_timezone_name = ""
        self.resolved_fold: int | None = None

    def clean(self):
        cleaned = super().clean()
        if self.errors:
            return cleaned

        parsed_value = self.claim.parsed_value
        if isinstance(parsed_value, datetime) and parsed_value.tzinfo is not None:
            self.resulting_occurred_at = parsed_value
            return cleaned

        parsed = parse_timestamp_value(self.claim.raw_value)
        local_value = parsed.value
        if isinstance(local_value, date) and not isinstance(local_value, datetime):
            raise forms.ValidationError(
                "A partial or date-only claim cannot be promoted to an exact "
                "occurrence time."
            )
        if not isinstance(local_value, datetime):
            raise forms.ValidationError(
                "This raw timestamp could not be parsed as a complete local date "
                "and time."
            )
        if local_value.tzinfo is not None:
            self.resulting_occurred_at = local_value
            return cleaned

        timezone_name = (cleaned.get("timezone_name") or "").strip()
        if not timezone_name:
            self.add_error(
                "timezone_name",
                "Enter an IANA timezone for this offset-free timestamp.",
            )
            return cleaned
        try:
            selected_zone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            self.add_error(
                "timezone_name",
                "Enter a recognized IANA timezone, such as America/Toronto.",
            )
            return cleaned

        valid_folds = _valid_local_folds(local_value, selected_zone)
        if not valid_folds:
            self.add_error(
                "timezone_name",
                "That local time does not exist in this timezone because of a "
                "clock change.",
            )
            return cleaned

        fold = cleaned.get("fold")
        offsets = {candidate.utcoffset() for candidate in valid_folds.values()}
        ambiguous = len(offsets) > 1
        if ambiguous and fold is None:
            self.add_error(
                "fold",
                "Choose the first or second offset for this ambiguous local time.",
            )
            return cleaned
        if fold is not None and fold not in valid_folds:
            self.add_error("fold", "That offset is not valid for this local time.")
            return cleaned

        selected_fold = fold if fold is not None else min(valid_folds)
        self.resulting_occurred_at = valid_folds[selected_fold]
        self.resolved_timezone_name = selected_zone.key
        self.resolved_fold = selected_fold if ambiguous else None
        return cleaned


def _valid_local_folds(local_value: datetime, zone: ZoneInfo) -> dict[int, datetime]:
    """Return folds that survive a UTC round-trip for one naive wall time."""
    valid: dict[int, datetime] = {}
    for fold in (0, 1):
        candidate = local_value.replace(tzinfo=zone, fold=fold)
        round_tripped = candidate.astimezone(UTC).astimezone(zone)
        if (
            round_tripped.replace(tzinfo=None) == local_value
            and round_tripped.fold == fold
        ):
            valid[fold] = candidate
    # Outside a fold, Python may normalize the round-trip to fold=0 only. That
    # one representation is sufficient and keeps the stored decision unambiguous.
    if not valid:
        candidate = local_value.replace(tzinfo=zone, fold=0)
        if (
            candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
            == local_value
        ):
            valid[0] = candidate
    return valid


class SubjectForm(forms.ModelForm):
    """Create a local gallery subject; no outside identity is consulted."""

    class Meta:
        model = Subject
        fields = ("label", "notes")
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}
        labels = {"label": "Person or subject name", "notes": "Notes"}
        help_texts = {
            "label": "A name you will recognize in your local reference gallery.",
            "notes": "Optional context to help distinguish this subject.",
        }


class SubjectReferenceForm(forms.Form):
    """Select an approved local observation as a subject reference."""

    subject = forms.ModelChoiceField(
        queryset=Subject.objects.none(),
        label="Person or subject",
        help_text="Choose who this observation will help identify in later items.",
        empty_label="Choose a person or subject",
    )
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        label="Reference notes",
        help_text="Optional. Explain why this is a useful reference for the subject.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["subject"].queryset = Subject.objects.order_by("label", "id")


class CandidateDecisionForm(forms.Form):
    """Capture an explicit confirmation or rejection and optional rationale."""

    decision = forms.ChoiceField(
        choices=(
            (IdentityCandidate.Decision.CONFIRMED, "Confirm association"),
            (IdentityCandidate.Decision.REJECTED, "Reject candidate"),
        ),
        widget=forms.HiddenInput,
    )
    note = forms.CharField(
        required=False,
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 2}),
    )


class RemoteConsentForm(forms.Form):
    """Require the operator to submit the exact manifest they reviewed."""

    manifest_hash = forms.CharField(widget=forms.HiddenInput, max_length=64)
    notice_version = forms.CharField(widget=forms.HiddenInput, max_length=64)
    acknowledge_transfer = forms.BooleanField(
        label=(
            "I consent to this exact item-specific transfer and understand that "
            "model output is uncertain."
        )
    )
