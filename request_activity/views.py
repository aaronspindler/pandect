"""Read-only request activity for authorized site operators."""

import os
from datetime import UTC, date, datetime, time, timedelta
from sqlite3 import Error as SQLiteError
from uuid import UUID

from django import forms
from django.conf import settings
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponseNotAllowed
from django.template.response import TemplateResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables

from .spool import STALE_HEARTBEAT_SECONDS, read_health
from .store import Reader, StoreUnavailable, normalize_fingerprint, normalize_ip

OPERATOR_GROUP = "Request activity operators"
PAGE_SIZE = 50
HEARTBEAT_GRACE = timedelta(seconds=STALE_HEARTBEAT_SECONDS)


def can_view_request_activity(user):
    return (
        settings.PANDECT_REQUEST_ACTIVITY_ENABLED
        and user.is_active
        and user.is_staff
        and user.groups.filter(name=OPERATOR_GROUP).exists()
    )


class RequestSearchForm(forms.Form):
    ip = forms.CharField(label="Client IP address", max_length=45, required=False)
    fingerprint = forms.CharField(
        label="Versioned fingerprint",
        max_length=75,
        required=False,
        help_text="Paste the complete value, including its version prefix.",
    )
    start = forms.DateField(
        label="From date (UTC)",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    end = forms.DateField(
        label="Through date (UTC)",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    cursor = forms.CharField(required=False, max_length=75, widget=forms.HiddenInput)

    def clean_ip(self):
        value = self.cleaned_data["ip"]
        if value:
            try:
                return normalize_ip(value)
            except ValueError as error:
                raise forms.ValidationError(
                    "Enter a valid IPv4 or IPv6 address."
                ) from error
        return None

    def clean_fingerprint(self):
        value = self.cleaned_data["fingerprint"]
        if value:
            try:
                return normalize_fingerprint(value)
            except ValueError as error:
                raise forms.ValidationError(
                    "Enter a complete versioned fingerprint."
                ) from error
        return None

    def clean_cursor(self):
        value = self.cleaned_data["cursor"]
        if value:
            try:
                timestamp, event_id = value.split("|", maxsplit=1)
                occurred_at = datetime.fromisoformat(timestamp)
                if occurred_at.tzinfo is None:
                    raise ValueError
                return occurred_at.astimezone(UTC), UUID(event_id)
            except (OverflowError, ValueError) as error:
                raise forms.ValidationError(
                    "Start a new search to reset the page."
                ) from error
        return None

    def clean(self):
        data = super().clean()
        start = data.get("start")
        end = data.get("end")
        if end == date.max:
            self.add_error("end", "Enter a date before 31 December 9999.")
        elif start and end and end < start:
            self.add_error("end", "Enter a date on or after the start date.")
        return data


def _capture_health():
    path = os.environ.get("PANDECT_REQUEST_ACTIVITY_SPOOL")
    if not path:
        return None
    try:
        state = read_health(path)
        if state is None:
            return None
        for field in ("heartbeat_at", "last_gap_at", "queued_oldest_at"):
            if state[field]:
                state[field] = parse_datetime(state[field])
        heartbeat = state["heartbeat_at"]
        state["stale"] = (
            heartbeat is None or timezone.now() - heartbeat > HEARTBEAT_GRACE
        )
        return state
    except KeyError, OSError, SQLiteError, TypeError, ValueError:
        return None


def _midnight(day):
    return datetime.combine(day, time.min, UTC) if day else None


@sensitive_variables("context", "filters", "page")
def _query_activity(context, filters):
    try:
        with Reader() as reader:
            page = reader.lookup(
                ip=filters.get("ip"),
                fingerprint=filters.get("fingerprint"),
                after=_midnight(filters.get("start")),
                before=_midnight(filters["end"] + timedelta(days=1))
                if filters.get("end")
                else None,
                cursor=filters.get("cursor"),
                limit=PAGE_SIZE,
            )
            context["page"] = page
            if page.next_cursor is not None:
                occurred_at, event_id = page.next_cursor
                context["next_cursor"] = f"{occurred_at.isoformat()}|{event_id}"
                context["filters"] = {
                    key: str(value)
                    for key, value in filters.items()
                    if key != "cursor" and value is not None
                }
            try:
                context["summary"] = reader.repeat_summary()
            except StoreUnavailable:
                context["summary_unavailable"] = True
    except StoreUnavailable:
        context["store_unavailable"] = True


@sensitive_post_parameters()
@sensitive_variables("context", "form")
def request_activity(request):
    if not settings.PANDECT_REQUEST_ACTIVITY_ENABLED:
        raise Http404
    if not can_view_request_activity(request.user):
        raise PermissionDenied
    if request.method not in {"GET", "HEAD", "POST"}:
        return HttpResponseNotAllowed(("GET", "HEAD", "POST"))

    form = RequestSearchForm(request.POST if request.method == "POST" else None)
    context = {
        **admin.site.each_context(request),
        "title": "Request activity",
        "search_form": form,
    }
    response_status = 200
    if not form.is_bound or form.is_valid():
        context["capture"] = _capture_health()
        _query_activity(context, form.cleaned_data if form.is_bound else {})
        if context.get("store_unavailable"):
            response_status = 503
    else:
        response_status = 400
    return TemplateResponse(
        request, "request_activity/admin_activity.html", context, status=response_status
    )
