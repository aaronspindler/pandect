"""Publish the two observed directions independently against comparable evidence."""

from django.core.files.base import ContentFile
from django.db.models import Q
from django.utils import timezone

from core.storage import canonical_bytes
from document.models import DocumentEvidence
from socialmedia.instagram_rosters import (
    LIVE_VERSION,
    normalize_members,
    roster_additions,
    validate_endpoint_coverage,
    validate_handle,
    validate_identifier,
)
from socialmedia.models import InstagramCapture, InstagramComparison, InstagramSource

COLLECTOR_VERSIONS = {
    InstagramSource.SYNTHETIC: "synthetic-browser-v1",
    InstagramSource.INSTAGRAM: LIVE_VERSION,
}


def publish_captures(check, observations, *, originals):
    """Called under the worker's authorization locks and retain_uploaded_originals."""
    monitor = check.monitor
    connection = monitor.connection
    if not isinstance(observations, dict):
        raise ValueError("invalid_observation")
    observer = validate_handle(observations.get("observer"))
    target = validate_handle(observations.get("target"))
    observer_id = validate_identifier(observations.get("observer_id"))
    target_id = validate_identifier(observations.get("target_id"))
    version = observations.get("collector_version")
    if (
        connection.user_id != monitor.created_by_id
        or connection.source != monitor.source
        or observations.get("source", monitor.source) != monitor.source
        or observer.lower() != connection.observer.lower()
        or connection.observer_id_string
        and observer_id != connection.observer_id_string
        or target.lower() != monitor.target.lower()
        or monitor.target_id
        and target_id != monitor.target_id
        or monitor.source not in COLLECTOR_VERSIONS
        or version != COLLECTOR_VERSIONS[monitor.source]
    ):
        raise ValueError("observation_source_changed")

    target_identity = f"id:{target_id}" if target_id else f"handle:{target.lower()}"
    source_label = monitor.get_source_display()
    source_url = (
        f"https://www.instagram.com/{target.lower()}/"
        if monitor.source == InstagramSource.INSTAGRAM
        else ""
    )
    captured_at = timezone.now()
    for direction, label in InstagramCapture.Direction.choices:
        result = observations.get(direction)
        if not isinstance(result, dict):
            raise ValueError("invalid_observation")
        complete = result.get("complete")
        reason = result.get("reason")
        if not isinstance(complete, bool) or (
            reason != "" if complete else reason not in InstagramCapture.Reason.values
        ):
            raise ValueError("invalid_observation")
        entries = normalize_members(result.get("members"))
        manifest = {
            "schema": "pandect.instagram-capture.v1",
            "source": monitor.source,
            "collector_version": version,
            "observer": observer,
            "observer_id": observer_id,
            "target": target,
            "target_id": target_id,
            "direction": direction,
            "started_at": check.started_at.isoformat(),
            "finished_at": captured_at.isoformat(),
            "complete": complete,
            "reason": reason,
            "members": entries,
        }
        if source_url:
            manifest["source_url"] = source_url
            pages, expected = validate_endpoint_coverage(result, len(entries))
            manifest["page_count"] = pages
            if expected is not None:
                manifest["expected_count"] = expected
        evidence = DocumentEvidence.objects.create(
            case_id=monitor.case_id,
            uploaded_by_id=monitor.created_by_id,
            title=f"{source_label} · @{target} · {label}",
            source=f"{source_label} monitor",
            source_url=source_url,
            collected_at=captured_at,
            declared_content_type="application/json",
            file=ContentFile(canonical_bytes(manifest), name=f"{direction}.json"),
        )
        originals.append(evidence.file)
        capture = InstagramCapture.objects.create(
            monitor_check=check,
            direction=direction,
            evidence=evidence,
            observer_epoch=connection.observer_epoch,
            target_identity=target_identity,
            collector_version=version,
            complete=complete,
            reason=reason,
            entries=entries,
            entry_count=len(entries),
        )
        if not capture.baseline_eligible:
            continue
        previous = (
            InstagramCapture.objects.filter(
                Q(complete=True)
                | Q(
                    collector_version=LIVE_VERSION,
                    reason=InstagramCapture.Reason.MISMATCH,
                ),
                monitor_check__monitor=monitor,
                monitor_check__status="complete",
                evidence__case_id=monitor.case_id,
                direction=direction,
            )
            .exclude(monitor_check=check)
            .order_by("-monitor_check__started_at", "-pk")
            .first()
        )
        if previous and (
            previous.target_identity == target_identity
            and previous.observer_epoch == connection.observer_epoch
            and previous.collector_version == version
        ):
            additions = roster_additions(previous.entries, entries)
            InstagramComparison.objects.create(
                capture=capture,
                previous=previous,
                additions=additions,
                addition_count=len(additions),
            )
