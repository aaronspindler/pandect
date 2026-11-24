"""Personal Instagram connections and case-authorized, deferred monitor checks."""

import logging
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from functools import partial
from uuid import uuid4

from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.http import Http404
from django.utils import timezone

from cases.models import Case
from cases.permissions import get_case
from core.storage import retain_uploaded_originals
from socialmedia import instagram_connector as connector
from socialmedia.instagram_rosters import HANDLE
from socialmedia.models import (
    InstagramCheck,
    InstagramConnection,
    InstagramMonitor,
    InstagramSource,
    InstagramTargetApproval,
)
from socialmedia.monitor_evidence import publish_captures
from socialmedia.tasks import check_instagram_monitor

logger = logging.getLogger(__name__)

# Keep the historical recovery grace for already retained deliveries.
RECOVERY_GRACE = timedelta(minutes=11)

MESSAGES = {
    "access_removed": "The investigator no longer has verified case edit access.",
    "access_lost": "The connected account can no longer view this target.",
    "challenge": "Reconnect the observing account to complete sign-in.",
    "cooldown": "The collection source requested a longer wait before another check.",
    "disconnected": "Reconnect the observing account before resuming.",
    "interrupted": "The previous check was interrupted. A replacement was queued.",
    "invalid_response": "The collection source returned an unusable observation.",
    "not_configured": "Scheduled Instagram checks are unavailable on this install.",
    "pairing_expired": "Start a new connection attempt.",
    "pairing_pending": "Finish signing in on the host browser, then confirm again.",
    "source_changed": "The collection source has changed. Review the connection.",
    "pilot_scope": "This pilot is unavailable for this investigator or case.",
    "target_unavailable": "The pilot does not permit this target and check interval.",
    "target_not_approved": "The case owner can approve this target in Case Settings.",
    "paused": "Collection was paused before the result was saved.",
    "stopped": "Collection was stopped before the result was saved.",
    "unavailable": "The collection service is unavailable. Resume after it is ready.",
    "failed": "The check could not be saved. Review the connection and resume.",
}


def check_message(code):
    return MESSAGES.get(code, MESSAGES["failed"])


def _validation_error(code):
    code = code if code in MESSAGES else "failed"
    return ValidationError(MESSAGES[code], code=code)


def _verified(user):
    if (
        not user.is_active
        or not EmailAddress.objects.filter(user=user, verified=True).exists()
    ):
        raise PermissionDenied("A verified active account is required.")


def _pairing_source():
    if not connector.scheduling_available():
        raise _validation_error("not_configured")
    return InstagramSource.SYNTHETIC


@contextmanager
def _connection_change(user_id):
    """Serialize personal browser replacements without holding a DB transaction."""
    # PostgreSQL's two-integer advisory keys are separate from worker slots.
    parts = [
        int.from_bytes(user_id.bytes[n : n + 4], "big", signed=True) for n in (0, 4)
    ]
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s, %s)", parts)
        try:
            yield
        finally:
            cursor.execute("SELECT pg_advisory_unlock(%s, %s)", parts)


def start_pairing(user):
    with _connection_change(user.pk):
        _verified(user)
        source = _pairing_source()
        try:
            pair = connector.start_pairing(str(user.pk))
        except connector.ConnectorError as error:
            raise _validation_error(error.code) from None
        return {**pair, "id": {"source": source, "token": pair["id"]}}


def _retire(monitors, status, reason):
    ids = monitors.values("pk")
    InstagramCheck.objects.filter(monitor_id__in=ids, status="running").update(
        status="cancelled", error=reason, finished_at=timezone.now()
    )
    monitors.update(status=status, next_check_at=None, generation=uuid4())


def _target_scope(case_id):
    return {
        "case_id": case_id,
        "source": InstagramSource.SYNTHETIC,
        "target_id": connector.PILOT_TARGET_ID,
    }


def target_approved(case_id):
    return InstagramTargetApproval.objects.filter(**_target_scope(case_id)).exists()


@transaction.atomic
def set_target_approval(user, case_id, *, approved):
    _verified(user)
    case = get_case(user, case_id, owner=True, lock=True)
    scope = _target_scope(case.pk)
    if approved:
        if not connector.scheduling_available():
            raise _validation_error("not_configured")
        InstagramTargetApproval.objects.get_or_create(**scope)
        return

    InstagramTargetApproval.objects.filter(**scope).delete()
    _retire(
        InstagramMonitor.objects.filter(**scope, status="active"),
        "paused",
        "target_not_approved",
    )


def _revoke_live(session):
    try:
        connector.revoke_live(session)
    except Exception:
        # The local credential is already gone. Keep upstream details out of logs.
        logger.warning("Could not revoke the previous Instagram browser session.")


def _replace_connection(conn, observer, session, observer_id="", *, source=None):
    if source is None:
        source = conn.source
    if (
        conn.source == "instagram"
        and conn.session
        and (conn.session != session or source != conn.source)
    ):
        transaction.on_commit(partial(_revoke_live, conn.session))
    _retire(
        InstagramMonitor.objects.filter(connection=conn, status="active"),
        "paused",
        "disconnected",
    )
    # Re-pairing establishes new observer provenance even for a reused handle.
    conn.source = source
    conn.observer = observer
    conn.observer_id_string = observer_id
    conn.session = session
    conn.observer_epoch = uuid4()
    conn.save()


def finish_pairing(user, pair_id):
    with _connection_change(user.pk):
        _verified(user)
        source = _pairing_source()
        # Previously stored pending IDs belong to the synthetic-only pilot.
        pending = (
            pair_id
            if isinstance(pair_id, dict)
            else {"source": "synthetic", "token": pair_id}
        )
        if pending.get("source") != source or not isinstance(pending.get("token"), str):
            raise _validation_error("pairing_expired")
        try:
            result = connector.finish_pairing(str(user.pk), pending["token"])
        except connector.ConnectorError as error:
            raise _validation_error(error.code) from None
        with transaction.atomic():
            user = get_user_model().objects.select_for_update().get(pk=user.pk)
            _verified(user)
            if (
                result.get("source", "synthetic") != source
                or _pairing_source() != source
            ):
                raise _validation_error("source_changed")
            conn, _ = InstagramConnection.objects.select_for_update().get_or_create(
                user=user
            )
            _replace_connection(
                conn,
                result["observer"],
                result["session"],
                result.get("observer_id", ""),
                source=source,
            )
            return conn


def disconnect(user):
    with _connection_change(user.pk), transaction.atomic():
        conn = InstagramConnection.objects.select_for_update().filter(user=user).first()
        if conn and conn.session:
            _replace_connection(conn, "", "")


def _schedule(monitor, due):
    monitor.generation = uuid4()
    monitor.status = "active"
    monitor.next_check_at = due
    monitor.save(update_fields=["generation", "status", "next_check_at"])
    check_instagram_monitor.using(queue_name="instagram", run_after=due).enqueue(
        str(monitor.pk), str(monitor.generation)
    )


def _collection_error(
    conn, user_id, case_id, source, target, target_id, interval_hours
):
    if not connector.scheduling_available():
        return "not_configured"
    if not conn or conn.user_id != user_id or not conn.session:
        return "disconnected"
    if conn.source != source or source != InstagramSource.SYNTHETIC:
        return "source_changed"
    if (
        not conn.observer_id_string
        or connector.connection_expires_at(conn.session) is None
    ):
        return "disconnected"
    if target_id != connector.PILOT_TARGET_ID or not connector.allowed_target(
        source, target, interval_hours
    ):
        return "target_unavailable"
    if not target_approved(case_id):
        return "target_not_approved"
    return ""


def _monitor_error(monitor):
    return _collection_error(
        monitor.connection,
        monitor.created_by_id,
        monitor.case_id,
        monitor.source,
        monitor.target,
        monitor.target_id,
        monitor.interval_hours,
    )


@transaction.atomic
def create_monitor(user, case_id, target, interval_hours):
    _verified(user)
    conn = InstagramConnection.objects.select_for_update().filter(user=user).first()
    case = get_case(user, case_id, edit=True, lock=True)
    handle = target.strip().removeprefix("@") if isinstance(target, str) else ""
    if not HANDLE.fullmatch(handle):
        raise ValidationError(
            "Use up to 30 letters, digits, periods or underscores in the handle."
        )
    target = handle.lower()
    if interval_hours not in (1, 6, 12, 24):
        raise ValidationError("Choose one of the available check intervals.")
    source = conn.source if conn else ""
    if error := _collection_error(
        conn,
        user.pk,
        case.pk,
        source,
        target,
        connector.PILOT_TARGET_ID,
        interval_hours,
    ):
        raise _validation_error(error)
    monitors = InstagramMonitor.objects.filter(
        case=case, connection=conn, source=source, target_id=connector.PILOT_TARGET_ID
    )
    existing = monitors.exclude(status="stopped").first()
    if existing:
        if existing.interval_hours != interval_hours:
            raise ValidationError(
                "This target already has a monitor with a different interval."
            )
        return existing
    monitor = monitors.create(
        case=case,
        connection=conn,
        source=source,
        target=target,
        target_id=connector.PILOT_TARGET_ID,
        created_by=user,
        interval_hours=interval_hours,
    )
    _schedule(monitor, timezone.now())
    return monitor


@contextmanager
def _collection_slot(conn_id):
    """Keep network I/O outside DB transactions; a dead worker releases its slot."""
    key = int.from_bytes(conn_id.bytes[:8], "big", signed=True)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [key])
        acquired = cursor.fetchone()[0]
    try:
        yield acquired
    finally:
        if acquired:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [key])


def _locked_monitor(monitor_id, case_id):
    # All writers lock the connection, case, then monitor. Disconnect can thus
    # retire every affected schedule without racing a simultaneous resume.
    rows = InstagramMonitor.objects.filter(pk=monitor_id, case_id=case_id)
    InstagramConnection.objects.select_for_update().filter(
        pk__in=rows.values("connection_id")
    ).exists()
    Case.objects.select_for_update().filter(pk=case_id).exists()
    return (
        rows.select_related("connection", "created_by")
        .select_for_update(of=("self",))
        .first()
    )


def check_needs_recovery(monitor, running, *, now):
    if monitor.status != "active":
        return False
    due = running.started_at if running else monitor.next_check_at
    return due is not None and due <= now - RECOVERY_GRACE


def _manage(user, case_id, monitor_id, action, *, interval_hours=None):
    get_case(user, case_id, edit=True)
    with transaction.atomic():
        monitor = _locked_monitor(monitor_id, case_id)
        case = get_case(user, case_id, edit=True)
        if not monitor:
            raise Http404
        if monitor.created_by_id != user.pk and not (
            action == "stop" and case.owner_id == user.pk
        ):
            raise PermissionDenied(
                "Only the investigator who connected the account can do this."
            )
        rows = InstagramMonitor.objects.filter(pk=monitor.pk)
        if action == "stop":
            _retire(rows, "stopped", "stopped")
        elif monitor.status == "stopped":
            raise ValidationError("This monitor has been stopped.")
        elif action == "pause":
            _retire(rows, "paused", "paused")
        elif action == "interval":
            _verified(user)
            error = _collection_error(
                monitor.connection,
                user.pk,
                case.pk,
                monitor.source,
                monitor.target,
                monitor.target_id,
                interval_hours,
            )
            if error:
                raise _validation_error(error)
            monitor.interval_hours = interval_hours
            monitor.save(update_fields=["interval_hours"])
        else:
            _verified(user)
            if error := _monitor_error(monitor):
                raise _validation_error(error)
            if action == "resume" and monitor.status == "paused":
                _schedule(monitor, timezone.now())
            elif action == "recover" and monitor.status == "active":
                with _collection_slot(monitor.connection_id) as acquired:
                    if not acquired:
                        raise ValidationError(
                            "A check is still running on this connection."
                        )
                    now = timezone.now()
                    running = monitor.checks.filter(
                        generation=monitor.generation, status="running"
                    ).first()
                    if not check_needs_recovery(monitor, running, now=now):
                        raise ValidationError(
                            "This check is within its scheduled window."
                        )
                    monitor.checks.filter(status="running").update(
                        status="failed", error="interrupted", finished_at=now
                    )
                    _schedule(monitor, now)
        monitor.refresh_from_db()
        return monitor


def pause_monitor(user, case_id, monitor_id):
    return _manage(user, case_id, monitor_id, "pause")


def resume_monitor(user, case_id, monitor_id):
    return _manage(user, case_id, monitor_id, "resume")


def stop_monitor(user, case_id, monitor_id):
    return _manage(user, case_id, monitor_id, "stop")


def recover_monitor(user, case_id, monitor_id):
    return _manage(user, case_id, monitor_id, "recover")


def change_interval(user, case_id, monitor_id, interval_hours):
    return _manage(user, case_id, monitor_id, "interval", interval_hours=interval_hours)


def _current(monitor_id, case_id, generation):
    monitor = _locked_monitor(monitor_id, case_id)
    if (
        monitor
        and monitor.status == "active"
        and str(monitor.generation) == str(generation)
    ):
        return monitor
    return None


def _permission_error(monitor):
    try:
        get_case(monitor.created_by, monitor.case_id, edit=True)
        _verified(monitor.created_by)
    except Http404, PermissionDenied:
        return "access_removed"
    return _monitor_error(monitor)


def _finish(check, status, error=""):
    check.status, check.error, check.finished_at = status, error, timezone.now()
    check.save(update_fields=["status", "error", "finished_at"])


def _claim(monitor_id, case_id, generation, slot):
    with transaction.atomic():
        monitor = _current(monitor_id, case_id, generation)
        if (
            not monitor
            or not monitor.next_check_at
            or monitor.next_check_at > timezone.now()
        ):
            return None
        error = _permission_error(monitor)
        if not error:
            cooldown = monitor.connection.cooldown_until
            if cooldown and cooldown > timezone.now():
                _schedule(monitor, cooldown)
                return None
            if not slot:
                _schedule(monitor, timezone.now() + timedelta(minutes=2))
                return None
        check = InstagramCheck.objects.create(
            monitor=monitor,
            generation=generation,
            status="running",
            started_at=timezone.now(),
        )
        monitor.next_check_at = None
        monitor.save(update_fields=["next_check_at"])
        if error:
            _finish(check, "failed", error)
            _retire(InstagramMonitor.objects.filter(pk=monitor.pk), "paused", error)
            return None
        return check


def _complete(check, observation, error="", retry_after=None):
    with retain_uploaded_originals() as originals:
        monitor = _current(check.monitor_id, check.monitor.case_id, check.generation)
        if not monitor:
            return "cancelled"
        blocked = _permission_error(monitor)
        error = blocked or error
        if not error:
            check.monitor = monitor
            publish_captures(check, observation, originals=originals)
            _finish(check, "complete")
            _schedule(monitor, timezone.now() + timedelta(hours=monitor.interval_hours))
            return "complete"
        _finish(check, "failed", error)
        if error == "cooldown":
            seconds = (
                retry_after
                if isinstance(retry_after, int) and retry_after > 0
                else 86400
            )
            try:
                due = timezone.now() + timedelta(seconds=seconds)
            except OverflowError:
                due = datetime.max.replace(tzinfo=UTC)
            InstagramConnection.objects.filter(pk=monitor.connection_id).update(
                cooldown_until=due
            )
            _schedule(
                monitor,
                max(due, timezone.now() + timedelta(hours=monitor.interval_hours)),
            )
            return "cooldown"
        if not blocked and error in {"challenge", "disconnected"}:
            _replace_connection(monitor.connection, "", "")
            return "failed"
        _retire(InstagramMonitor.objects.filter(pk=monitor.pk), "paused", error)
        return "failed"


def run_check(monitor_id, generation):
    ids = (
        InstagramMonitor.objects.filter(
            pk=monitor_id, generation=generation, status="active"
        )
        .values_list("connection_id", "case_id")
        .first()
    )
    if not ids:
        return "cancelled"
    conn_id, case_id = ids
    with _collection_slot(conn_id) as slot:
        check = _claim(monitor_id, case_id, generation, slot)
        if not check:
            return "unavailable"
        retry_after = None
        try:
            observation = connector.collect(
                check.monitor.connection.session, check.monitor.target
            )
            return _complete(check, observation)
        except connector.ConnectorError as error:
            code = error.code if error.code in MESSAGES else "failed"
            retry_after = error.retry_after
        except ValueError:
            code = "invalid_response"
        except Exception:
            # DB task results persist tracebacks. Keep collector details out of them.
            code = "failed"
        return _complete(check, None, code, retry_after)
