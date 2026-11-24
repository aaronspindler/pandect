"""Incremental local outlines, checkpointed on the existing PostgreSQL queue."""

from uuid import NAMESPACE_URL, uuid5

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Avg, F, Q
from django.http import Http404
from django.tasks import task

from core.case_search.indexing import authorize
from core.case_search.parent_material import material, origins
from core.case_search.parents import VERSION
from core.case_search.semantic_model import CHUNK_BYTES, profile
from core.models import SearchParent, SearchParentScan, SearchPassage
from core.models.search_parents import PARENT_ORIGINS

ORIGINS = tuple(PARENT_ORIGINS)
PAGE = 32


def _pooled(case, anchors, identity):
    """Pool only existing approved passage vectors, locally and at one profile."""
    if not identity:
        return None
    spans = Q(pk__in=[])
    for ref in anchors:
        start = ref["start"]
        spans |= Q(
            **{f"document__{ref['source']}_id": ref["id"]},
            start__lt=start + ref["length"],
            start__gte=max(0, start - CHUNK_BYTES),
            end__gt=start,
        )
    return (
        SearchPassage.objects.alias(end=F("start") + F("length"))
        .filter(
            spans,
            case=case,
            document__case=case,
            document__complete=True,
            profile=identity,
            document__profile=identity,
        )
        .aggregate(vector=Avg("embedding"))["vector"]
    )


@task
@transaction.atomic
def index_parent(case_id, actor_id, source, key, offset=0):
    try:
        case = authorize(actor_id, case_id, edit=True, lock=True)
    except Http404, PermissionDenied:
        return
    rows, following = material(case, source, key, offset)
    identity = profile()
    for position, fields in rows:
        if not fields["anchors"]:
            continue
        keys = {f"{r['source']}:{r['id']}" for r in fields["anchors"]}
        pk = uuid5(
            NAMESPACE_URL, f"pandect-search-parent:{case.pk}:{source}:{key}:{position}"
        )
        vector = _pooled(
            case,
            fields["anchors"],
            identity,
        )
        SearchParent.objects.update_or_create(
            pk=pk,
            defaults={
                "case": case,
                f"{source}_id": key,
                "version": VERSION,
                **fields,
                "vector_sources": sorted(keys),
                "profile": identity if vector is not None else "",
                "embedding": vector,
            },
        )
    if following is not None:
        index_parent.enqueue(str(case.pk), str(actor_id), source, str(key), following)


@task
@transaction.atomic
def refresh_parent_vectors(case_id, actor_id, keys, cursor=None):
    try:
        case = authorize(actor_id, case_id, edit=True, lock=True)
    except Http404, PermissionDenied:
        return
    identity = profile()
    if not identity:
        return
    rows = SearchParent.objects.filter(
        case=case, version=VERSION, vector_sources__overlap=keys
    ).order_by("pk")
    if cursor:
        rows = rows.filter(pk__gt=cursor)
    batch = list(rows.defer("words", "outline", "embedding")[:PAGE])
    for parent in batch:
        vector = _pooled(case, parent.anchors, identity)
        parent.profile = identity if vector is not None else ""
        parent.embedding = vector
    SearchParent.objects.bulk_update(batch, ["profile", "embedding"])
    if len(batch) == PAGE:
        refresh_parent_vectors.enqueue(case_id, actor_id, keys, str(batch[-1].pk))


@task
@transaction.atomic
def index_children(case_id, actor_id, source, scope, cursor=None):
    """Queue small batches when a grouping or extraction run publishes many rows."""
    try:
        case = authorize(actor_id, case_id, edit=True, lock=True)
    except Http404, PermissionDenied:
        return
    rows = origins(case, source).filter(**scope).order_by("pk").distinct()
    if cursor:
        rows = rows.filter(pk__gt=cursor)
    ids = list(rows.values_list("pk", flat=True)[:PAGE])
    for pk in ids:
        index_parent.enqueue(case_id, actor_id, source, str(pk))
    if len(ids) == PAGE:
        index_children.enqueue(case_id, actor_id, source, scope, str(ids[-1]))


@task
@transaction.atomic
def scan_parents(scan_id, actor_id, version, phase, cursor):
    scan = SearchParentScan.objects.filter(pk=scan_id).first()
    if not scan or phase >= len(ORIGINS):
        return
    try:
        case = authorize(actor_id, scan.case_id, edit=True, lock=True)
    except Http404, PermissionDenied:
        return
    scan = (
        SearchParentScan.objects.select_for_update()
        .filter(pk=scan.pk, version=version, phase=phase, cursor=cursor)
        .first()
    )
    if not scan or version != VERSION:
        return
    source = ORIGINS[phase]
    rows = origins(case, source).order_by("pk")
    if cursor:
        rows = rows.filter(pk__gt=cursor)
    ids = list(rows.values_list("pk", flat=True)[:PAGE])
    for pk in ids:
        index_parent.enqueue(str(case.pk), actor_id, source, str(pk))
    scan.cursor = ids[-1] if len(ids) == PAGE else None
    if scan.cursor is None:
        scan.phase += 1
    scan.save(update_fields=("phase", "cursor"))
    if scan.phase < len(ORIGINS):
        scan_parents.enqueue(
            str(scan.pk),
            actor_id,
            VERSION,
            scan.phase,
            str(scan.cursor) if scan.cursor else None,
        )


@transaction.atomic
def prepare_parents(case, actor, *, retry=False):
    case = authorize(actor.pk, case.pk, edit=True, lock=True)
    scan, created = SearchParentScan.objects.get_or_create(
        case=case, defaults={"version": VERSION}
    )
    if scan.version != VERSION or (retry and scan.phase >= len(ORIGINS)):
        scan.version, scan.phase, scan.cursor = VERSION, 0, None
        scan.save(update_fields=("version", "phase", "cursor"))
    elif not created and not retry:
        return False
    scan_parents.enqueue(
        str(scan.pk),
        str(actor.pk),
        VERSION,
        scan.phase,
        str(scan.cursor) if scan.cursor else None,
    )
    return True


def published_parent_sources(result):
    args = str(result.case_id), str(result.run.requested_by_id)
    if result.passages.exists():
        index_children.enqueue(*args, "extraction", {"result_id": str(result.pk)})
    if (
        result.run.operation == "session_summaries"
        or result.run.parameters.get("_search_generation") == "briefs"
    ):
        index_parent.enqueue(*args, "summary", str(result.pk))


def archive_parent_sources(case, actor, kind, item_id, *, archived):
    """Invalidate affected outlines and reseed them on restore."""
    from core.models import AnalysisResult
    from phone.models import ConversationSessionMember

    scope = [{"kind": kind, "evidence_id": str(item_id)}]
    results = AnalysisResult.objects.filter(
        case=case, run__case=case, run__inputs__contains=scope
    )
    selections = [
        ("summary", {"run__inputs__contains": scope}),
        ("extraction", {"result__run__inputs__contains": scope}),
    ]
    affected = Q(summary__in=results) | Q(extraction__result__in=results)
    if kind == "text":
        affected |= Q(text_id=item_id)
        selections.append(("text", {"pk": str(item_id)}))
    if kind in ("phone-call", "facetime"):
        field = "phone_call_id" if kind == "phone-call" else "facetime_id"
        affected |= Q(**{f"attachment__{field}": item_id})
        selections.append(("attachment", {field: str(item_id)}))
    if kind in ("document", "text-message"):
        field = (
            "message_id"
            if kind == "text-message"
            else "message__communication_context__source_document_id"
        )
        members = ConversationSessionMember.objects.filter(
            case=case, **{field: item_id}
        )
        affected |= Q(session_id__in=members.values("session_id"))
        selections.append(("session", {f"members__{field}": str(item_id)}))
    if archived:
        SearchParent.objects.filter(affected, case=case).delete()
    else:
        for source, query in selections:
            index_children.enqueue(str(case.pk), str(actor.pk), source, query)
