from uuid import UUID

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import F, Prefetch
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import (
    require_http_methods,
    require_POST,
    require_safe,
)

from cases.permissions import accessible_cases, case_context, get_case
from config.access import product_login_required
from socialmedia import instagram_connector, monitor_jobs
from socialmedia.models import (
    InstagramCapture,
    InstagramComparison,
    InstagramConnection,
    InstagramMonitor,
)


class MonitorForm(forms.Form):
    target = forms.CharField(
        label="Target handle",
        max_length=31,
        initial=instagram_connector.PILOT_TARGET,
        help_text="The synthetic collector supports @demo.account.",
    )
    interval_hours = forms.TypedChoiceField(
        label="Check interval",
        coerce=int,
        choices=InstagramMonitor.INTERVALS,
        initial=24,
    )


def _connection(user):
    retained = (
        InstagramConnection.objects.filter(user=user)
        .exclude(session="")
        .values("source", "observer", "observer_id_string", "session")
        .first()
    )
    if not retained:
        return None
    binding = retained.pop("session")
    observer_id = retained.pop("observer_id_string")
    expires_at = (
        instagram_connector.connection_expires_at(binding)
        if retained["source"] == "synthetic" and observer_id
        else None
    )
    return retained | {"expires_at": expires_at, "ready": bool(expires_at)}


def _monitor_setup(case, user, *, can_edit):
    available = instagram_connector.scheduling_available()
    return {
        "available": available,
        "target_approved": available and monitor_jobs.target_approved(case.pk),
        "connection": _connection(user) if can_edit else None,
    }


@product_login_required
@require_http_methods(["GET", "POST"])
def connection_settings(request):
    case = None
    if case_id := request.GET.get("case"):
        try:
            case = accessible_cases(request.user).filter(pk=UUID(case_id)).first()
        except ValueError:
            pass
    available = instagram_connector.scheduling_available()
    pending_key = "instagram_pilot_pair"
    pending = request.session.get(pending_key)
    pending_source = pending.get("source") if isinstance(pending, dict) else "synthetic"
    if pending_source != "synthetic" or not available:
        pending = None
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "start":
                if not pending:
                    pair = monitor_jobs.start_pairing(request.user)
                    request.session[pending_key] = pair["id"]
            elif action == "finish":
                if not pending:
                    raise ValidationError("Start a new connection and try again.")
                monitor_jobs.finish_pairing(request.user, pending)
                request.session.pop(pending_key, None)
                messages.success(request, "Instagram connection saved.")
                if case:
                    return redirect("instagram:list", case.pk)
            elif action == "cancel":
                request.session.pop(pending_key, None)
            elif action == "disconnect":
                monitor_jobs.disconnect(request.user)
                request.session.pop(pending_key, None)
                messages.success(
                    request, "Instagram disconnected. Monitoring has stopped."
                )
            else:
                raise Http404
        except ValidationError as exc:
            if action == "finish" and getattr(exc, "code", "") == "pairing_expired":
                request.session.pop(pending_key, None)
            messages.error(request, " ".join(exc.messages))
        return redirect(request.get_full_path())
    return render(
        request,
        "socialmedia/monitor_connection.html",
        {
            "return_case": case,
            "available": available,
            "pending": bool(pending),
            "connection": _connection(request.user),
        },
    )


@product_login_required
@require_safe
def monitor_list(request, case_id):
    case = get_case(request.user, case_id)
    context = case_context(case, request.user)
    monitors = InstagramMonitor.objects.filter(case=case).order_by("-created_at", "-pk")
    return render(
        request,
        "socialmedia/monitor_list.html",
        context
        | _monitor_setup(case, request.user, can_edit=context["can_edit"])
        | {"page_obj": Paginator(monitors, 25).get_page(request.GET.get("page"))},
    )


@product_login_required
@require_http_methods(["GET", "POST"])
def monitor_create(request, case_id):
    case = get_case(request.user, case_id, edit=True)
    form = MonitorForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            monitor = monitor_jobs.create_monitor(
                request.user, case.pk, **form.cleaned_data
            )
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, "Monitor saved.")
            return redirect("instagram:detail", case.pk, monitor.pk)
    return render(
        request,
        "socialmedia/monitor_create.html",
        case_context(case, request.user)
        | _monitor_setup(case, request.user, can_edit=True)
        | {"form": form},
        status=400 if request.method == "POST" else 200,
    )


@product_login_required
@require_safe
def monitor_detail(request, case_id, monitor_id):
    case = get_case(request.user, case_id)
    monitor = get_object_or_404(InstagramMonitor, case=case, pk=monitor_id)
    captures = InstagramCapture.objects.filter(evidence__case=case).defer("entries")
    checks = monitor.checks.prefetch_related(Prefetch("captures", queryset=captures))
    history = Paginator(checks, 10).get_page(request.GET.get("page"))
    for check in history:
        check.message = monitor_jobs.check_message(check.error) if check.error else ""
    if history.number == 1:
        latest = next(iter(history), None)
        last_success = next(
            (check for check in history if check.status == "complete"), None
        )
    else:
        latest = checks.first()
        last_success = latest if latest and latest.status == "complete" else None
    if last_success is None and history.paginator.num_pages > 1:
        last_success = checks.filter(status="complete").first()
    latest_captures = (
        {item.direction: item for item in last_success.captures.all()}
        if last_success
        else {}
    )
    # A comparison reads both sources. Enforce both relationships on every read.
    comparisons = (
        InstagramComparison.objects.filter(
            capture__monitor_check__monitor=monitor,
            capture__evidence__case=case,
            previous__monitor_check__monitor=monitor,
            previous__evidence__case=case,
        )
        .select_related("capture__monitor_check", "previous__monitor_check")
        .annotate(first_handle=F("additions__0__handle"))
        .defer("additions", "capture__entries", "previous__entries")
        .order_by("-capture__monitor_check__started_at", "-pk")
    )
    changes = []
    for direction, title in (
        ("followers", "New followers"),
        ("following", "New accounts followed"),
    ):
        direction_comparisons = comparisons.filter(capture__direction=direction)
        latest_comparison = direction_comparisons.first()
        earlier_additions = direction_comparisons.filter(addition_count__gt=0)
        if latest_comparison:
            earlier_additions = earlier_additions.exclude(pk=latest_comparison.pk)
        changes.append(
            {
                "direction": direction,
                "title": title,
                "comparison": latest_comparison,
                "capture": latest_captures.get(direction),
                "earlier": Paginator(earlier_additions, 5).get_page(
                    request.GET.get(direction)
                ),
            }
        )
    running = (
        latest
        if latest
        and latest.generation == monitor.generation
        and latest.status == "running"
        else None
    )
    recoverable = monitor_jobs.check_needs_recovery(
        monitor, running, now=timezone.now()
    )
    context = case_context(case, request.user)
    creator = request.user.pk == monitor.created_by_id
    setup = _monitor_setup(case, request.user, can_edit=creator and context["can_edit"])
    available = setup["available"] and monitor.source == "synthetic"
    can_schedule = (
        available
        and setup["target_approved"]
        and setup["connection"]
        and setup["connection"]["ready"]
    )
    return render(
        request,
        "socialmedia/monitor_detail.html",
        context
        | setup
        | {
            "monitor": monitor,
            "creator": creator,
            "last_success": last_success,
            "latest_failed_check": latest
            if latest and latest.status == "failed"
            else None,
            "latest_issue": monitor_jobs.check_message(latest.error)
            if latest
            and latest.error
            and (latest.status == "failed" or monitor.status == "paused")
            else "",
            "history": history,
            "changes": changes,
            "running": bool(running),
            "recoverable": recoverable,
            "intervals": InstagramMonitor.INTERVALS if can_schedule else (),
            "available": available,
            "can_schedule": can_schedule,
        },
    )


@product_login_required
@require_safe
def capture(request, case_id, monitor_id, capture_id):
    case = get_case(request.user, case_id)
    item = get_object_or_404(
        InstagramCapture.objects.select_related("monitor_check__monitor"),
        pk=capture_id,
        monitor_check__monitor__case=case,
        monitor_check__monitor_id=monitor_id,
        evidence__case=case,
    )
    comparison = (
        InstagramComparison.objects.filter(
            capture=item,
            previous__monitor_check__monitor_id=monitor_id,
            previous__evidence__case=case,
        )
        .select_related("previous__monitor_check")
        .defer("previous__entries")
        .first()
    )
    changes = request.GET.get("show") == "changes"
    members = item.entries
    if changes:
        members = comparison.additions if comparison else []
    return render(
        request,
        "socialmedia/monitor_capture.html",
        case_context(case, request.user)
        | {
            "monitor": item.monitor_check.monitor,
            "capture": item,
            "comparison": comparison,
            "show_changes": changes,
            "page_obj": Paginator(members, 25).get_page(request.GET.get("page")),
        },
    )


@product_login_required
@require_POST
def monitor_action(request, case_id, monitor_id, action):
    actions = {
        "pause": (monitor_jobs.pause_monitor, "Monitor paused."),
        "resume": (monitor_jobs.resume_monitor, "Monitor resumed."),
        "stop": (monitor_jobs.stop_monitor, "Monitor stopped."),
        "recover": (monitor_jobs.recover_monitor, "A replacement check is queued."),
    }
    if action not in actions and action != "interval":
        raise Http404
    case = get_case(request.user, case_id, edit=True)
    monitor = get_object_or_404(InstagramMonitor, case=case, pk=monitor_id)
    try:
        if action == "stop" and request.POST.get("confirm_stop") != "1":
            raise ValidationError("Confirm that you want to stop future checks.")
        if action == "recover" and request.POST.get("worker_stopped") != "1":
            raise ValidationError(
                "Confirm the previous worker has stopped before recovering this check."
            )
        if action == "interval":
            interval = forms.TypedChoiceField(
                coerce=int, choices=InstagramMonitor.INTERVALS
            ).clean(request.POST.get("interval_hours"))
            monitor_jobs.change_interval(request.user, case.pk, monitor.pk, interval)
            notice = "Check interval saved."
        else:
            operation, notice = actions[action]
            operation(request.user, case.pk, monitor.pk)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, notice)
    return redirect("instagram:detail", case.pk, monitor.pk)
