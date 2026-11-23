"""Exact, case-scoped deletion inventories shared by product and admin."""

import hashlib
import json
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from django.core import signing
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.db.models.deletion import Collector, ProtectedError
from django.http import StreamingHttpResponse

from cases.permissions import get_case
from core.analysis.operations import OPERATIONS
from core.evidence.queries import get_evidence, same_case_evidence
from core.evidence.types import KIND_LABELS, KINDS
from core.models import AnalysisRun, ImportantItem, ImportantNote, SavedSelection
from geo.models import (
    VehicleLocationEvidence,
    VehicleLocationImport,
    VehicleLocationImportRow,
    VehicleTripGeneration,
)
from phone.models import CommunicationContext
from socialmedia.models import InstagramCapture, InstagramComparison

CASE_CONFLICT = (
    "Related material has inconsistent case links. "
    "Ask an operator to correct them before deleting this evidence."
)
CHANGED = (
    "What will be removed has changed. Review the updated inventory and confirm again."
)
SALT = "core.evidence.deletion-inventory"
MODEL_KINDS = {model: kind for kind, model in KINDS.items()}
INVENTORY_LABELS = {
    "analysisfinding": "Searchable finding",
    "analysisfindingsource": "Finding source reference",
    "analysisartifact": "Generated file record",
    "extractedpassage": "Extracted text",
    "gatewayrequest": "Processing usage",
    "communicationcontext": "Imported message source context",
    "callevidenceattachment": "Retained call material",
    "instagramcapture": "Instagram list capture",
    "instagramcomparison": "Instagram list comparison",
    "importantitem": "Important item",
    "importantnote": "Important item note",
}


@dataclass
class DeletionPlan:
    items: list
    kind: str
    collector: Collector
    entries: list = field(default_factory=list)
    counts: Counter = field(default_factory=Counter)
    fingerprint: str = ""
    blocker: str = ""
    analysis_count: int = 0
    affected_vehicle_ids: set = field(default_factory=set)

    @property
    def item(self):
        return self.items[0]

    def revision(self, actor):
        return signing.dumps(self._identity(actor), salt=SALT)

    def _identity(self, actor):
        return [
            str(actor.pk),
            str(self.item.case_id),
            self.kind,
            sorted(str(item.pk) for item in self.items),
            self.fingerprint,
        ]

    def matches(self, revision, actor):
        try:
            return signing.loads(revision, salt=SALT) == self._identity(actor)
        except signing.BadSignature, TypeError, ValueError:
            return False


def _analysis_runs(case_id, selections, generation_ids):
    # Scan retained selections once rather than issue one JSON query for every
    # observation in a CSV (which can contain 100,000 rows).
    runs = AnalysisRun.objects.filter(case_id=case_id)
    result_ids = {}
    dependents = defaultdict(set)
    ids = set()
    for run_id, operation, inputs, parameters, result_id in runs.values_list(
        "pk", "operation", "inputs", "parameters", "result__pk"
    ).iterator():
        if result_id is not None:
            result_ids[run_id] = str(result_id)
        # Removing a generation also removes trips built from its other rows.
        if (
            generation_ids
            and operation == "journey_claims"
            and any(
                isinstance(group, dict)
                and group.get("origin") == "trip"
                and isinstance(group.get("generation_id"), str)
                and group.get("generation_id") in generation_ids
                for group in parameters.get("groups", [])
            )
        ):
            ids.add(run_id)
        for source in inputs:
            if (source.get("kind"), source.get("evidence_id")) in selections:
                ids.add(run_id)
            if upstream := source.get("result_id"):
                dependents[upstream].add(run_id)
        for ref in parameters.get("finding_refs", []):
            if isinstance(ref, dict) and (upstream := ref.get("result_id")):
                dependents[upstream].add(run_id)
    pending = deque(ids)
    while pending:
        result_id = result_ids.get(pending.popleft())
        for run_id in dependents.get(result_id, ()):
            if run_id not in ids:
                ids.add(run_id)
                pending.append(run_id)
    return runs.filter(pk__in=ids).only("id", "case_id", "operation", "status")


def _outside_case(model, objects, case_id):
    ids = [obj.pk for obj in objects]
    if not ids:
        return False
    scopes = {
        VehicleLocationImportRow: ("batch__case_id",),
        ImportantNote: ("item__case_id",),
        InstagramCapture: ("monitor_check__monitor__case_id", "evidence__case_id"),
        InstagramComparison: (
            "capture__monitor_check__monitor__case_id",
            "capture__evidence__case_id",
            "previous__monitor_check__monitor__case_id",
            "previous__evidence__case_id",
        ),
    }.get(model, ("case_id",))
    return (
        model.objects.filter(pk__in=ids)
        .exclude(**dict.fromkeys(scopes, case_id))
        .exists()
    )


def _record_rows(model, objects):
    """Fetch only inventory labels/identities, never original bodies or raw CSV JSON."""
    fields = {f.name for f in model._meta.fields}
    wanted = fields.intersection(
        {
            "title",
            "is_archived",
            "file",
            "original_filename",
            "sha256",
            "audio_sha256",
            "kind",
            "operation",
            "status",
            "number",
            "source_row",
            "batch",
            "run",
            "result",
            "message",
            "evidence_kind",
            "evidence_id",
            "finding_result",
            "item_index",
            "item",
        }
    )
    return model.objects.filter(pk__in=[obj.pk for obj in objects]).values(
        "id", *wanted
    )


def _record_name(model, row):
    if model in MODEL_KINDS:
        return row["title"] + (" · Archived" if row["is_archived"] else "")
    if model is ImportantItem:
        if row["finding_result"]:
            return f"Finding {row['item_index'] + 1} · result {row['finding_result']}"
        return f"{KIND_LABELS[row['evidence_kind']]} evidence · {row['evidence_id']}"
    if model is ImportantNote:
        return f"Note · important item {row['item']}"
    if model is AnalysisRun:
        operation = OPERATIONS.get(row["operation"])
        return (
            (operation.label if operation else "Analysis")
            + " · "
            + dict(AnalysisRun.Status.choices).get(row["status"], row["status"])
        )
    if model is VehicleLocationImportRow:
        return f"CSV review row {row['number']} · import {row['batch']}"
    if model is CommunicationContext:
        return (
            f"Imported message context · row {row['source_row']} · "
            f"message {row['message']}"
        )
    if row.get("original_filename"):
        return row["original_filename"]
    if row.get("run"):
        return f"{model._meta.verbose_name} · analysis {row['run']}"
    if row.get("result"):
        return f"{model._meta.verbose_name} · result {row['result']}"
    return str(model._meta.verbose_name).capitalize()


def _case_file(name, case_id):
    path = PurePosixPath(name)
    return (
        "\\" not in name
        and path.as_posix() == name
        and ".." not in path.parts
        and name.startswith(f"evidence/cases/{case_id}/")
    )


def plan_deletion(item, kind):
    return plan_deletions([item], kind)


def plan_deletions(items, kind):
    """Plan one evidence type within one case, collecting shared dependents once."""
    item = items[0]
    if any(source.case_id != item.case_id for source in items):
        raise ValueError("A deletion plan must belong to one case.")
    collector = Collector(using=item._state.db, origin=item, force_collection=True)
    plan = DeletionPlan(items, kind, collector)
    roots = {kind: items}
    batches = []
    if kind == "document":
        batches = list(
            VehicleLocationImport.objects.filter(document__in=items).only(
                "id", "case_id"
            )
        )
        roots["vehicle-location"] = list(
            VehicleLocationEvidence.objects.filter(import_batch__in=batches).only(
                "id", "case_id"
            )
        )
        roots["text-message"] = list(
            KINDS["text-message"]
            .objects.filter(communication_context__source_document__in=items)
            .only("id", "case_id")
        )
    # Check source-linked roots before querying or displaying any private labels.
    if any(
        obj.case_id != item.case_id for objects in roots.values() for obj in objects
    ) or any(batch.case_id != item.case_id for batch in batches):
        plan.blocker = CASE_CONFLICT
        return plan
    for root_kind, objects in roots.items():
        model = KINDS[root_kind]
        ids = [obj.pk for obj in objects]
        if same_case_evidence(model.objects.filter(pk__in=ids)).count() != len(ids):
            plan.blocker = CASE_CONFLICT
            return plan
    vehicle_roots = roots.get("vehicle-location", [])
    vehicle_ids = [source.pk for source in vehicle_roots]
    generations = VehicleTripGeneration.objects.filter(
        inputs__observation_id__in=vehicle_ids
    ).distinct()
    if generations.exclude(case_id=item.case_id).exists():
        plan.blocker = CASE_CONFLICT
        return plan
    runs = _analysis_runs(
        item.case_id,
        {
            (root_kind, str(obj.pk))
            for root_kind, objects in roots.items()
            for obj in objects
        },
        {str(pk) for pk in generations.values_list("pk", flat=True)},
    )
    plan.analysis_count = len(runs)
    plan.affected_vehicle_ids = set(
        VehicleLocationEvidence.objects.filter(pk__in=vehicle_ids).values_list(
            "vehicle_id", flat=True
        )
    )
    marked_evidence = Q()
    for root_kind, objects in roots.items():
        if objects:
            marked_evidence |= Q(
                evidence_kind=root_kind, evidence_id__in=[obj.pk for obj in objects]
            )
    try:
        for objects in roots.values():
            if objects:
                collector.collect(objects, fail_on_restricted=False)
        if batches:
            collector.collect(batches, fail_on_restricted=False)
        collector.collect(runs, fail_on_restricted=False)
        collector.collect(generations, fail_on_restricted=False)
        # Evidence kinds have separate tables, so these targets use typed UUIDs.
        # Collect before the scope checks to detect inconsistent case links.
        collector.collect(
            ImportantItem.objects.filter(marked_evidence), fail_on_restricted=False
        )
    except ProtectedError:
        plan.blocker = (
            "Related material has a protected relationship. "
            "Ask an operator to review it."
        )
        return plan
    # RESTRICT is retained on models. The explicit, confirmed plan includes the
    # dependent import records and evidence before resolving those restrictions.
    for model, objects in collector.data.items():
        collector.clear_restricted_objects_from_set(model, objects)
    restricted = [
        obj
        for fields in collector.restricted_objects.values()
        for objects in fields.values()
        for obj in objects
    ]
    if restricted:
        plan.blocker = (
            "Related material could not be included safely. "
            "Ask an operator to review its source links."
        )
        return plan
    for model, objects in collector.data.items():
        if _outside_case(model, objects, item.case_id):
            plan.blocker = CASE_CONFLICT
            return plan
    updates = []
    for (db_field, value), groups in collector.field_updates.items():
        for group in groups:
            objects = list(group)
            if _outside_case(db_field.model, objects, item.case_id):
                plan.blocker = CASE_CONFLICT
                return plan
            removed = {obj.pk for obj in collector.data.get(db_field.model, [])}
            updates.extend(
                {
                    "action": "Update",
                    "type": str(db_field.model._meta.verbose_name),
                    "name": f"Clear {db_field.verbose_name} reference",
                    "id": str(obj.pk),
                    "field": db_field.name,
                    "value": value,
                }
                for obj in objects
                if obj.pk not in removed
            )
    private_files = []
    for model, objects in collector.data.items():
        for row in _record_rows(model, objects):
            name = _record_name(model, row)
            label = (
                KIND_LABELS[MODEL_KINDS[model]] + " evidence"
                if model in MODEL_KINDS
                else INVENTORY_LABELS.get(
                    model._meta.model_name, str(model._meta.verbose_name).capitalize()
                )
            )
            entry = {
                "action": "Delete record",
                "type": label,
                "name": name,
                "id": str(row["id"]),
            }
            plan.entries.append(entry)
            plan.counts["records"] += 1
            if row.get("file"):
                if not _case_file(row["file"], item.case_id):
                    plan.blocker = CASE_CONFLICT
                    plan.entries.clear()
                    return plan
                private_files.append(row["file"])
                file_name = row.get("original_filename") or (
                    "Generated " + row.get("kind", "file").replace("_", " ")
                )
                plan.entries.append(
                    {
                        "action": "Delete file",
                        "type": label,
                        "name": file_name,
                        "id": str(row["id"]),
                        "sha256": row.get("audio_sha256") or row.get("sha256", ""),
                    }
                )
                plan.counts["files"] += 1
    plan.entries.extend(updates)
    plan.counts["updates"] = len(updates)
    impacts = selection_deletion_impacts(
        item.case_id,
        {
            (root_kind, str(obj.pk))
            for root_kind, objects in roots.items()
            for obj in objects
        },
        runs.values_list("result__pk", flat=True),
    )
    for impact in impacts:
        plan.entries.append(
            {
                "action": "Retain unavailable references",
                "type": "Shared selection",
                "name": impact["name"],
                "id": impact["id"],
                "revision": impact["revision"],
                "members": impact["members"],
            }
        )
    plan.entries.sort(
        key=lambda row: (row["action"], row["type"], row["id"], row["name"])
    )
    digest = hashlib.sha256()
    for row in [*plan.entries, *sorted(private_files)]:
        digest.update(json.dumps(row, sort_keys=True, default=str).encode())
        digest.update(b"\n")
    plan.fingerprint = digest.hexdigest()
    return plan


@transaction.atomic
def execute_deletion(plan, actor):
    """Remove the reviewed inventory and invalidate remaining vehicle history."""
    if plan.blocker or not actor.is_superuser:
        raise PermissionDenied
    case = get_case(actor, plan.item.case_id, lock=True)
    plan.collector.delete()
    if plan.affected_vehicle_ids:
        from geo.trip_processing import invalidate_vehicle_trips

        invalidate_vehicle_trips(actor, case, plan.affected_vehicle_ids)


def deletion_review_context(request, plan):
    reviewed = request.GET.get("review_revision")
    changed = bool(reviewed and not plan.matches(reviewed, request.user))
    page = Paginator(plan.entries, 50).get_page(
        1 if changed else request.GET.get("review_page")
    )
    params = request.GET.copy()
    params.pop("download", None)
    params["review_revision"] = plan.revision(request.user)
    links = {}
    for label, number in (
        ("previous", page.previous_page_number() if page.has_previous() else None),
        ("next", page.next_page_number() if page.has_next() else None),
    ):
        if number is not None:
            params["review_page"] = number
            links[label] = "?" + params.urlencode()
    params.pop("review_page", None)
    params["download"] = "manifest"
    return {
        "deletion_plan": plan,
        "deletion_page": page,
        "deletion_links": links,
        "deletion_download_url": "?" + params.urlencode(),
        "deletion_revision": plan.revision(request.user),
        "inventory_changed": CHANGED if changed and request.method == "GET" else "",
    }


def deletion_manifest_response(plan):
    payload = {
        "schema": "pandect.evidence-deletion.v1",
        "case_id": str(plan.item.case_id),
        "fingerprint": plan.fingerprint,
        "evidence_id": str(plan.item.pk),
        "counts": dict(plan.counts),
        "entries": plan.entries,
    }
    # Linux WebKit can render JSON attachments inline; use a download MIME type.
    response = StreamingHttpResponse(
        json.JSONEncoder().iterencode(payload), content_type="application/octet-stream"
    )
    response["Content-Disposition"] = (
        f'attachment; filename="deletion-{plan.item.pk}.json"'
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def get_deletion_target(actor, case_id, kind, evidence_id, *, lock=False):
    case = get_case(actor, case_id, lock=lock)
    if not actor.is_superuser:
        raise PermissionDenied("Only superusers can permanently delete evidence.")
    return case, get_evidence(case, kind, evidence_id, lock=lock)


def selection_deletion_impacts(case_id, evidence_keys, result_ids):
    """Inventory references that will remain as unavailable selection members."""
    evidence_keys = {(kind, str(key)) for kind, key in evidence_keys}
    result_ids = {str(value) for value in result_ids}
    impacts = []
    for selection in SavedSelection.objects.filter(case_id=case_id).iterator(
        chunk_size=100
    ):
        affected = []
        for index, member in enumerate(selection.members):
            source = member.get("source", {})
            if (
                (source.get("kind"), source.get("evidence_id")) in evidence_keys
                or source.get("result_id") in result_ids
                or member.get("result_id") in result_ids
            ):
                affected.append({"index": index, "reference": member})
        if affected:
            impacts.append(
                {
                    "id": str(selection.pk),
                    "name": selection.name,
                    "revision": selection.revision,
                    "members": affected,
                }
            )
    return impacts
