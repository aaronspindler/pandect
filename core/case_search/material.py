"""Read a bounded window and canonical locator directly from the authorized case."""

from collections import defaultdict
from datetime import datetime

from django.db.models import Q
from django.urls import reverse

from core.evidence.queries import scoped_evidence
from core.evidence.types import KIND_LABELS
from core.evidence_chat import corpus
from socialmedia.models import SocialAccount

from .sources import dated, sources

RECORDS = {
    "person": (
        "people:detail",
        ("first_name", "last_name", "email", "phone_number", "address", "notes"),
    ),
    "vehicle": (
        "vehicle:detail",
        ("make", "model", "year", "color", "license_plate", "vin", "notes"),
    ),
    "poi": (
        "geo:poi_detail",
        ("kind", "custom_label", "latitude", "longitude", "address", "source", "notes"),
    ),
}
WINDOW = 2048


def surfaces():
    return (
        *corpus.surfaces(),
        *(("record", kind) for kind in RECORDS),
        ("social", "person"),
        ("report", ""),
        ("trip", "possible-trip"),
    )


def event_dates(case, source, locator):
    """Use the same primary date or trip interval as the case's structured filters."""
    if source.surface == "trip":
        return tuple(
            datetime.fromisoformat(locator[key]) for key in ("start_at", "end_at")
        )
    if source.surface in ("original", "passage", "attachment"):
        queryset = dated(sources(case, locator["kind"])).filter(
            pk=locator["evidence_id"]
        )
        field = "hit_date"
    else:
        queryset = rows(case, source.surface, source.kind).filter(pk=source.source_key)
        field = (
            "result__run__created_at" if source.surface == "finding" else "created_at"
        )
    stamp = queryset.values_list(field, flat=True).first()
    return (stamp, stamp) if stamp else None


def rows(case, surface, kind, through=None):
    if surface == "report":
        from core.evidence_chat.analysis_reports import eligible_results

        qs = eligible_results(case).exclude(
            run__operation__in=("evidence_chat", "narrative_relationships")
        )
        return qs.filter(created_at__lte=through) if through else qs
    if surface == "trip":
        from geo.trip_citations import citation_trips

        qs = citation_trips(case)
        return qs.filter(generation__finished_at__lte=through) if through else qs
    if surface == "social":
        qs = SocialAccount.objects.filter(person__case=case).select_related("person")
        return qs.filter(updated_at__lte=through) if through else qs
    if surface == "record":
        qs = dated(sources(case, kind))
        return qs.filter(updated_at__lte=through) if through else qs
    qs = corpus.rows(case, surface, kind, False, through)
    return (
        qs.exclude(result__run__operation="narrative_relationships")
        if surface == "finding"
        else qs
    )


def source_count(case):
    """Count the worker's discovery inventory before passage preparation."""
    first, *others = (
        rows(case, surface, kind).order_by().values_list("pk", flat=True)
        for surface, kind in surfaces()
    )
    # Each surface is separate work, even if two surfaces share a UUID.
    return first.union(*others, all=True).count()


def _read(case, source, start=0, through=None, filters=None):
    surface, kind = source.surface, source.kind
    qs = rows(case, surface, kind, through).filter(pk=source.source_key)
    if surface == "report":
        from core.evidence_chat.analysis_reports import read_analysis_material

        if filters and filters.get("record_type") not in (None, "", "report"):
            return None
        if filters and filters.get("start"):
            qs = qs.filter(created_at__gte=filters["start"])
        if filters and filters.get("end"):
            qs = qs.filter(created_at__lt=filters["end"])
        if not qs.exists():
            return None
        return read_analysis_material(
            case, source.source_key, start, WINDOW, through=through
        )
    if filters and surface not in ("record", "social", "trip"):
        from core.evidence_chat.search_scope import scope_rows

        values = {
            k: v
            for k, v in filters.items()
            if k in ("record_type", "end", "start") and v
        }
        if values.get("record_type") == "finding":
            if surface != "finding":
                return None
            values.pop("record_type")
        if surface == "finding":
            if after := values.get("start"):
                qs = qs.filter(result__run__created_at__gte=after)
            if before := values.get("end"):
                qs = qs.filter(result__run__created_at__lt=before)
        else:
            qs = scope_rows(qs, case, surface, kind, values, include_recordings=True)
    if filters and surface in ("record", "social", "trip"):
        if filters.get("record_type") and kind != filters["record_type"]:
            return None
        if after := filters.get("start"):
            qs = qs.filter(
                **{
                    (
                        {
                            "trip": "end_at",
                            "social": "created_at",
                            "record": "hit_date",
                        }[surface]
                    )
                    + "__gte": after
                }
            )
        if before := filters.get("end"):
            qs = qs.filter(
                **{
                    (
                        {
                            "trip": "start_at",
                            "social": "created_at",
                            "record": "hit_date",
                        }[surface]
                    )
                    + "__lt": before
                }
            )
    if surface == "trip":
        from core.evidence_chat.trips import read_trip_material

        if not qs.exists():
            return None
        return read_trip_material(
            case, source.source_key, start, WINDOW, through=through
        )
    if surface in ("record", "social"):
        row = qs.first()
        if row is None:
            return None
        return record_material(case, surface, kind, row, start, WINDOW)
    item = corpus.clipped(qs, corpus.body_field(surface, kind), start, WINDOW).first()
    if item is None:
        return None
    found = corpus.material(
        case,
        surface,
        kind,
        item,
        False,
        start,
        WINDOW,
        through=through,
        hosted_metadata=True,
    )
    if found and filters and surface == "finding":
        if filters.get("record_type") not in (
            None,
            "",
            "finding",
            found.locator["kind"],
        ):
            return None
    return found


def record_material(case, surface, kind, row, start, size):
    route, fields = RECORDS[kind]
    key = row.pk
    label = (
        f"Case {kind} record · {row.hit_title}"
        if surface == "record"
        else f"Reported social account · {row.person}"
    )
    if surface == "social":
        fields = ("platform", "username", "source")
        key = row.person_id
    details = [(f.replace("_", " "), getattr(row, f)) for f in fields]
    if surface == "social" or kind == "poi":
        details.insert(0, ("Reported person", str(row.person)))
    suffix = corpus.lines(details)
    url = reverse(route, args=(case.pk, key)) + (
        "#social-accounts" if surface == "social" else ""
    )
    found = corpus.make_material(
        "record",
        kind,
        key,
        row,
        [],
        label,
        url,
        version=[surface, str(key), str(row.updated_at)],
        suffix=suffix,
        start=start,
        size=size,
    )
    found.locator["record_surface"] = surface
    return found


def read_record(case, locator, pk, start, size):
    kind, surface = locator.get("kind"), locator.get("record_surface")
    if (
        kind not in RECORDS
        or surface not in ("record", "social")
        or (surface == "social" and kind != "person")
    ):
        return None
    row = rows(case, surface, kind).filter(pk=pk).first()
    return (
        record_material(case, surface, kind, row, start, size)
        if row is not None
        else None
    )


def read(case, source, start=0, through=None, filters=None):
    found = _read(case, source, start, through, filters)
    if found:
        dependencies = defaultdict(set)
        for ref in found.locator.get("dependencies", []):
            dependencies[ref["kind"]].add(ref["id"])
        for kind, ids in dependencies.items():
            originals = scoped_evidence(case, kind).filter(
                is_archived=False, pk__in=ids
            )
            if through:
                originals = originals.filter(
                    Q(archive_changed_at__isnull=True)
                    | Q(archive_changed_at__lte=through),
                    created_at__lte=through,
                )
            if originals.count() != len(ids):
                return None
    return found


def kind_label(locator):
    return {
        "report": "Saved analysis",
        "finding": "Finding",
        "passage": "Extracted passage",
        "trip": "Possible trip",
    }.get(locator["surface"], KIND_LABELS.get(locator["kind"], locator["kind"].title()))
