"""Exact, case-scoped deletion inventories shared by product and admin."""

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from itertools import batched, chain

from django.core import signing
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import connection, transaction
from django.db.models import Exists, F, OuterRef, Q
from django.db.models.deletion import Collector, ProtectedError
from django.db.models.functions import JSONObject
from django.http import StreamingHttpResponse
from django.utils import timezone

from cases.deletion import collected_querysets, outside_case
from cases.permissions import get_case
from core.analysis.operations import OPERATIONS
from core.evidence.queries import get_evidence, same_case_evidence
from core.evidence.types import CALL_KINDS, KIND_LABELS, KINDS
from core.models import (
    AnalysisRun,
    ChatSource,
    ChatTurn,
    DeletionReview,
    EvidenceChat,
    ImportantItem,
    ImportantNote,
    SavedSelection,
)
from core.storage import case_storage_key
from core.tasks import expire_deletion_review
from geo.models import (
    VehicleLocationEvidence,
    VehicleLocationImport,
    VehicleLocationImportRow,
    VehicleTrip,
    VehicleTripGeneration,
)
from phone.models import (
    CallHistoryImport,
    CallHistoryRow,
    CallPersonLink,
    CommunicationContext,
    ConversationSession,
    ConversationSessionMember,
)

CASE_CONFLICT = (
    "Related material has inconsistent case links. "
    "Ask an operator to correct them before deleting this evidence."
)
CHANGED = (
    "What will be removed has changed. Review the updated inventory and confirm again."
)
SALT = "core.evidence.deletion-inventory"
REVIEW_PAGE_SIZE = 50
MODEL_KINDS = {model: kind for kind, model in KINDS.items()}
INVENTORY_LABELS = {
    "analysisfinding": "Searchable finding",
    "analysisfindingsource": "Finding source reference",
    "analysisartifact": "Generated file record",
    "extractedpassage": "Extracted text",
    "gatewayrequest": "Processing usage",
    "communicationcontext": "Imported message source context",
    "callevidenceattachment": "Retained call material",
    "callhistoryimport": "Call history import",
    "callhistoryrow": "Call import review row",
    "callpersonlink": "Investigator call party link",
    "instagramcapture": "Instagram list capture",
    "instagramcomparison": "Instagram list comparison",
    "importantitem": "Important item",
    "importantnote": "Important item note",
    "chatturn": "Evidence chat question and reply",
    "chatanalysisproposal": "Evidence chat analysis proposal",
    "extractionreuse": "Shared extraction request",
    "chatsource": "Chat review source",
    "chatmatch": "Chat match and citation",
    "evidencechat": "Evidence chat",
}


@dataclass
class DeletionPlan:
    items: list
    kind: str
    collector: Collector | None
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


def _direct_run_ids(case_id, runs, selections, generation_ids, session_ids):
    if len(selections) + len(generation_ids) + len(session_ids) <= 64:
        match = Q(pk__in=[])
        for kind, key in selections:
            match |= Q(inputs__contains=[{"kind": kind, "evidence_id": key}])
        for key in generation_ids:
            match |= Q(
                operation="journey_claims",
                parameters__contains={
                    "groups": [{"origin": "trip", "generation_id": key}]
                },
            )
        for key in session_ids:
            match |= Q(parameters__contains={"_saved_session_range": {"session": key}})
        return set(runs.filter(match).values_list("pk", flat=True))

    # Large imports can contain 100,000 source IDs. Compare their typed keys in
    # PostgreSQL without one containment predicate per key or loading run JSON.
    kinds, keys = zip(*selections, strict=True) if selections else ([], [])
    with connection.cursor() as cursor:
        cursor.execute(
            """
            WITH selected(kind, key) AS (
                SELECT * FROM unnest(%s::text[], %s::text[])
            )
            SELECT r.id
            FROM core_analysisrun r
            WHERE r.case_id = %s AND (
                EXISTS (
                    SELECT 1 FROM jsonb_array_elements(r.inputs) AS source
                    JOIN selected
                      ON source->>'kind' = selected.kind
                     AND source->>'evidence_id' = selected.key
                )
                OR (
                    r.operation = 'journey_claims' AND EXISTS (
                        SELECT 1 FROM jsonb_array_elements(
                            COALESCE(r.parameters->'groups', '[]'::jsonb)
                        ) AS trip
                        WHERE trip->>'origin' = 'trip'
                          AND trip->>'generation_id' = ANY(%s::text[])
                    )
                )
                OR r.parameters->'_saved_session_range'->>'session' = ANY(%s::text[])
            )
            """,
            (list(kinds), list(keys), case_id, list(generation_ids), list(session_ids)),
        )
        return {run_id for (run_id,) in cursor}


def _analysis_runs(case_id, selections, generation_ids, session_ids):
    runs = AnalysisRun.objects.filter(case_id=case_id).order_by()
    ids = _direct_run_ids(case_id, runs, selections, generation_ids, session_ids)
    pending = ids.copy()
    while pending:
        found = set()
        for batch in batched(pending, 1000, strict=False):
            results = runs.filter(pk__in=batch, result__isnull=False).values_list(
                "result__pk", flat=True
            )
            for result_ids in batched(results.iterator(), 64, strict=False):
                dependent = Q()
                for result_id in result_ids:
                    ref = {"result_id": str(result_id)}
                    dependent |= Q(inputs__contains=[ref]) | Q(
                        parameters__contains={"finding_refs": [ref]}
                    )
                found.update(runs.filter(dependent).values_list("pk", flat=True))
        pending = found - ids
        ids.update(pending)
    return runs.filter(pk__in=ids).only("id", "case_id", "operation", "status")


def _record_rows(model, objects, using):
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
            "chat_turn",
            "item",
        }
    )
    for rows in collected_querysets(model, objects, using):
        yield from rows.values("id", *wanted).iterator(chunk_size=500)


def _hosted_deletion_entries(plan, collector):
    from core.models import HostedSearchIndex, HostedSearchItem

    indexes = HostedSearchIndex.objects.filter(
        case_key=plan.item.case_id, retired=False
    )
    if transaction.get_connection().in_atomic_block:
        indexes = indexes.select_for_update()
    active_ids = list(indexes.values_list("id", flat=True))
    if not active_ids:
        return
    surfaces = {
        "analysisresult": "report",
        "analysisfinding": "finding",
        "extractedpassage": "passage",
        "callevidenceattachment": "attachment",
        "vehicletrip": "trip",
    }
    for model, objects in collector.data.items():
        kind = MODEL_KINDS.get(model)
        surface = "original" if kind else surfaces.get(model._meta.model_name)
        if surface is None:
            continue
        receipts = HostedSearchItem.objects.filter(
            source__index_id__in=active_ids, source__surface=surface
        )
        if kind:
            receipts = receipts.filter(source__kind=kind)
        for group in collected_querysets(model, objects, collector.using):
            for pk in receipts.filter(
                source__source_key__in=group.values("pk")
            ).values_list("pk", flat=True):
                yield {
                    "action": "Queue hosted removal",
                    "type": "Cloudflare search passage",
                    "name": "Private passage and indexing receipt",
                    "id": str(pk),
                }


def _record_name(model, row):
    if model in MODEL_KINDS:
        return row["title"] + (" · Archived" if row["is_archived"] else "")
    if model is ImportantItem:
        if row["chat_turn"]:
            return f"Chat reply · {row['chat_turn']}"
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
    if model in (VehicleLocationImportRow, CallHistoryRow):
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
    call_batches = []
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
            .objects.filter(
                Q(communication_context__source_document__in=items)
                | Q(group_source__in=items)
            )
            .only("id", "case_id")
        )
        call_batches = list(
            CallHistoryImport.objects.filter(document__in=items).only("id", "case_id")
        )
        for call_kind in CALL_KINDS:
            roots[call_kind] = list(
                KINDS[call_kind]
                .objects.filter(import_row__batch__in=call_batches)
                .only("id", "case_id")
            )
    # Check source-linked roots before querying or displaying any private labels.
    if any(
        obj.case_id != item.case_id for objects in roots.values() for obj in objects
    ) or any(batch.case_id != item.case_id for batch in (*batches, *call_batches)):
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
    message_ids = [source.pk for source in roots.get("text-message", [])]
    sessions = ConversationSession.objects.filter(
        members__message_id__in=message_ids
    ).distinct()
    if sessions.exclude(case_id=item.case_id).exists():
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
        {str(pk) for pk in sessions.values_list("pk", flat=True)},
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
    if vehicle_ids:
        # Removing a contributing original retires its whole trip generation.
        marked_evidence |= Q(
            evidence_kind="possible-trip",
            evidence_id__in=VehicleTrip.objects.filter(
                generation__in=generations
            ).values("pk"),
        )
    # Every derived surface retains its analysis result. Deleting one of its
    # inputs also deletes that result through the dependency walk above.
    affected_sources = ChatSource.objects.filter(
        marked_evidence | Q(result_id__in=runs.values("result__pk"))
    )
    affected = Q(pk__in=affected_sources.values("turn_id"))
    from core.models import ChatSelectionMember

    selected_roots = Q()
    for root_kind, objects in roots.items():
        if objects:
            selected_roots |= Q(
                kind=root_kind, evidence_id__in=[obj.pk for obj in objects]
            )
    if selected_roots:
        # All later answers from this thread are included below, including
        # replies that reused the original selection for a new comparison.
        affected |= Q(
            pk__in=ChatSelectionMember.objects.filter(
                selected_roots, turn__case=item.case, turn__chat__case=item.case
            ).values("turn_id")
        )
    if message_ids:
        from core.evidence_chat.recorded import deletion_dependencies

        affected |= Q(pk__in=deletion_dependencies(item.case, message_ids))
    affected_turns = ChatTurn.objects.filter(affected)
    # A follow-up may quote or paraphrase its earlier conversation.
    downstream = ChatTurn.objects.filter(
        Exists(
            affected_turns.filter(
                chat_id=OuterRef("chat_id"), ordinal__lte=OuterRef("ordinal")
            )
        )
    )
    try:
        for objects in roots.values():
            if objects:
                collector.collect(objects, fail_on_restricted=False)
        if batches:
            collector.collect(batches, fail_on_restricted=False)
        if call_batches:
            collector.collect(call_batches, fail_on_restricted=False)
        elif kind in CALL_KINDS:
            collector.collect(
                CallPersonLink.objects.filter(
                    row_id__in=KINDS[kind]
                    .objects.filter(pk__in=[call.pk for call in items])
                    .values("import_row_id")
                ),
                fail_on_restricted=False,
            )
        collector.collect(runs, fail_on_restricted=False)
        collector.collect(downstream, fail_on_restricted=False)
        collector.collect(
            EvidenceChat.objects.filter(
                pk__in=affected_turns.filter(ordinal=1).values("chat_id")
            ),
            fail_on_restricted=False,
        )
        collector.collect(generations, fail_on_restricted=False)
        if message_ids:
            # Include every member of each affected session in the reviewed
            # inventory. An individual source deletion cannot shorten a session.
            members = ConversationSessionMember.objects.filter(
                session__in=sessions
            ).only("pk", "case_id")
            collector.collect(list(members), fail_on_restricted=False)
            collector.collect(sessions, fail_on_restricted=False)
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
        if any(
            outside_case(rows, item.case_id)
            for rows in collected_querysets(model, objects, collector.using)
        ):
            plan.blocker = CASE_CONFLICT
            return plan
    updates = []
    for (db_field, value), groups in collector.field_updates.items():
        for group in groups:
            if any(
                outside_case(rows, item.case_id)
                for rows in collected_querysets(db_field.model, group, collector.using)
            ):
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
                for obj in group
                if obj.pk not in removed
            )
    private_files = []
    for model, objects in collector.data.items():
        for row in _record_rows(model, objects, collector.using):
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
                if not case_storage_key(
                    row["file"], item.case_id, model_name=model._meta.model_name
                ):
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
    plan.entries.extend(_hosted_deletion_entries(plan, collector))
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
    for row in chain(plan.entries, sorted(private_files)):
        digest.update(json.dumps(row, sort_keys=True, default=str).encode())
        digest.update(b"\n")
    plan.fingerprint = digest.hexdigest()
    return plan


@transaction.atomic
def execute_deletion(plan, actor):
    """Remove the reviewed inventory and invalidate remaining vehicle history."""
    if plan.blocker or plan.collector is None or not actor.is_superuser:
        raise PermissionDenied
    case = get_case(actor, plan.item.case_id, lock=True)
    DeletionReview.objects.filter(case=case).delete()
    plan.collector.delete()
    if plan.affected_vehicle_ids:
        from geo.trip_processing import invalidate_vehicle_trips

        invalidate_vehicle_trips(actor, case, plan.affected_vehicle_ids)


class ReviewedDeletionEntries:
    def __init__(self, review):
        self.review = review

    def count(self):
        return self.review.entry_count

    def __getitem__(self, page_slice):
        return self.review.page_entries


def load_deletion_review(request, case, item, kind):
    reviewed = request.GET.get("review_revision")
    if not reviewed or not request.GET.get("review_page"):
        return None
    try:
        page = int(request.GET["review_page"])
    except ValueError:
        return None
    if not 1 <= page < 2**31 or _review_in_flight(case):
        return None
    review = (
        DeletionReview.objects.filter(
            case=case,
            actor=request.user,
            kind=kind,
            evidence_id=item.pk,
            expires_at__gt=timezone.now(),
            versions=JSONObject(
                **{
                    name: F(f"case__activity__{name}")
                    for name in case.activity.versions()
                }
            ),
        )
        .defer("pages")
        .annotate(page_entries=F(f"pages__{page - 1}"))
        .first()
    )
    if review and review.page_entries:
        plan = DeletionPlan(
            [item],
            kind,
            None,
            entries=ReviewedDeletionEntries(review),
            counts=review.counts,
            fingerprint=review.fingerprint,
            analysis_count=review.analysis_count,
        )
        if plan.matches(reviewed, request.user):
            return plan
    return None


@transaction.atomic
def save_deletion_review(case, actor, plan):
    if plan.blocker or len(plan.entries) <= REVIEW_PAGE_SIZE:
        return
    versions = case.activity.versions()
    current = get_case(actor, case.pk, lock=True)
    # The joined activity row can have been read before waiting for the case lock.
    current.activity.refresh_from_db()
    if current.activity.versions() != versions or _review_in_flight(current):
        return
    now = timezone.now()
    DeletionReview.objects.filter(case=case, expires_at__lte=now).delete()
    expires_at = now + timedelta(minutes=10)
    review, _ = DeletionReview.objects.update_or_create(
        case=case,
        actor=actor,
        kind=plan.kind,
        evidence_id=plan.item.pk,
        defaults={
            "fingerprint": plan.fingerprint,
            "versions": versions,
            "counts": dict(plan.counts),
            "analysis_count": plan.analysis_count,
            "entry_count": len(plan.entries),
            "pages": [
                list(page)
                for page in batched(plan.entries, REVIEW_PAGE_SIZE, strict=False)
            ],
            "expires_at": expires_at,
        },
    )
    expire_deletion_review.using(run_after=expires_at).enqueue(str(review.pk))


def _review_in_flight(case):
    # Gateway usage can be added while a run or chat turn is already active,
    # without moving its activity revision again until the stage completes.
    return AnalysisRun.objects.filter(case=case, status="running").exists() or (
        ChatTurn.objects.filter(case=case, status="running").exists()
    )


def deletion_review_context(request, plan):
    reviewed = request.GET.get("review_revision")
    changed = bool(reviewed and not plan.matches(reviewed, request.user))
    page = Paginator(plan.entries, REVIEW_PAGE_SIZE).get_page(
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
