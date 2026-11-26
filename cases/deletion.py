"""Check the scope of an ORM deletion before exposing or applying its cascade."""

from collections import defaultdict
from itertools import batched

from django.db import router
from django.db.models import FileField, QuerySet
from django.db.models.deletion import Collector

from core.storage import case_storage_key

CASCADE_CONFLICT = (
    "Related records have inconsistent case links. "
    "Ask an operator to review them before deleting this record."
)

CASE_PATHS = {
    "cases.case": ("pk",),
    "core.chatanalysisproposal": (
        "parent__case_id",
        "parent__chat__case_id",
        "document__case_id",
    ),
    "core.extractionreuse": ("case_id", "run__case_id"),
    "core.chatturn": ("case_id", "chat__case_id"),
    "core.chatsource": ("case_id", "turn__case_id", "turn__chat__case_id"),
    "core.chatmatch": (
        "case_id",
        "turn__case_id",
        "source__case_id",
        "source__turn__case_id",
    ),
    "core.importantnote": ("item__case_id",),
    "geo.pointofinterest": ("person__case_id",),
    "geo.vehiclelocationimportrow": ("batch__case_id",),
    "phone.callhistoryimport": ("case_id", "source__case_id", "document__case_id"),
    "phone.callhistoryrow": (
        "batch__case_id",
        "batch__source__case_id",
        "batch__document__case_id",
    ),
    "phone.callpersonlink": (
        "row__batch__case_id",
        "row__batch__source__case_id",
        "row__batch__document__case_id",
        "person__case_id",
    ),
    "phone.conversationsession": ("case_id", "grouping__case_id"),
    "phone.conversationsessionmember": (
        "case_id",
        "session__case_id",
        "message__case_id",
    ),
    "socialmedia.socialaccount": ("person__case_id",),
    "socialmedia.instagramcheck": ("monitor__case_id",),
    "socialmedia.instagramcapture": (
        "monitor_check__monitor__case_id",
        "evidence__case_id",
    ),
    "socialmedia.instagramcomparison": (
        "capture__monitor_check__monitor__case_id",
        "capture__evidence__case_id",
        "previous__monitor_check__monitor__case_id",
        "previous__evidence__case_id",
    ),
}


class CaseDeletionBlocked(Exception):
    pass


def collected_querysets(model, objects, using):
    """Keep lazy cascades lazy and check collected instances in bounded batches."""
    if isinstance(objects, QuerySet):
        yield objects
        return
    manager = model._base_manager.using(using)
    for ids in batched((obj.pk for obj in objects), 500, strict=False):
        yield manager.filter(pk__in=ids)


def _grouped_rows(objects, using):
    by_model = defaultdict(list)
    for obj in objects:
        by_model[type(obj)].append(obj)
    for model, group in by_model.items():
        yield from collected_querysets(model, group, using)


def _case_paths(model):
    if model._meta.label_lower in CASE_PATHS:
        return CASE_PATHS[model._meta.label_lower]
    return ("case_id",) if hasattr(model, "case_id") else ()


def outside_case(rows, case_id):
    paths = _case_paths(rows.model)
    if not paths:
        return rows.exists()
    if rows.exclude(**dict.fromkeys(paths, case_id)).exists():
        return True
    if rows.model._meta.label_lower == "phone.textmessageevidence":
        from core.evidence.queries import same_case_evidence

        groups = rows.filter(group_source__isnull=False)
        return groups.exclude(pk__in=same_case_evidence(groups).values("pk")).exists()
    return False


def inaccessible_case_records(objects, cases, using):
    """Check which collected case records can be described to the current actor."""
    case_ids = cases.values("pk")
    for rows in _grouped_rows(objects, using):
        paths = _case_paths(rows.model)
        if (
            paths
            and rows.exclude(
                **dict.fromkeys((f"{path}__in" for path in paths), case_ids)
            ).exists()
        ):
            return True
    return False


def cascade_conflicts(collector, case_id, *, protected=()):
    for model, objects in collector.data.items():
        for rows in collected_querysets(model, objects, collector.using):
            if outside_case(rows, case_id):
                return True
            for field in model._meta.concrete_fields:
                if isinstance(field, FileField) and any(
                    not case_storage_key(
                        key, case_id, model_name=model._meta.model_name
                    )
                    for key in rows.exclude(**{field.name: ""})
                    .values_list(field.name, flat=True)
                    .iterator()
                ):
                    return True
    for rows in collector.fast_deletes:
        if outside_case(rows, case_id):
            return True
    for (field, _), groups in collector.field_updates.items():
        for group in groups:
            if any(
                outside_case(rows, case_id)
                for rows in collected_querysets(field.model, group, collector.using)
            ):
                return True
    return any(
        outside_case(rows, case_id)
        for rows in _grouped_rows(protected, collector.using)
    )


def delete_case_record(obj, case_id):
    """Delete under the case lock held by the authorized caller."""
    using = router.db_for_write(type(obj), instance=obj)
    collector = Collector(using=using, origin=obj)
    collector.collect([obj])
    if cascade_conflicts(collector, case_id):
        raise CaseDeletionBlocked(CASCADE_CONFLICT)
    collector.delete()
