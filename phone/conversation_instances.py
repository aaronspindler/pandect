"""Browse exact, source-validated inferred sessions and their saved summaries."""

from itertools import batched
from uuid import UUID

from django.contrib.postgres.expressions import ArraySubquery
from django.core.paginator import Paginator
from django.db.models import Exists, F, OuterRef, Q, Subquery
from django.http import Http404

from core.analysis.catalogue import SUCCESSFUL, finding_rows
from core.analysis.errors import AnalysisError
from core.analysis.navigation import finding_url
from core.analysis.presentation import participant_names, participant_text
from core.analysis.services import SourceResolutionCache, resolve_inputs
from core.models import AnalysisFindingSource, AnalysisRun
from phone.conversation_sessions import ordered_members, session_findings
from phone.conversations import imported_contexts

INSTANCES_PER_PAGE = 25
MESSAGES_PER_PAGE = 50
SUMMARY_STATEMENTS = 3


def _instance_id(value):
    try:
        return UUID(str(value))
    except ValueError, TypeError, AttributeError:
        raise Http404("Instance not found.") from None


def _candidates(case, messages):
    pair_ids = messages.order_by().values("pk")
    references = AnalysisFindingSource.objects.filter(
        case=case, finding_id=OuterRef("pk"), kind="text-message"
    )
    # Neither a citation from a different pair nor a page boundary may shorten
    # a group. Use originals for the displayed time, including unknown times.
    member_times = messages.filter(
        pk__in=AnalysisFindingSource.objects.filter(
            case=case, finding_id=OuterRef(OuterRef("pk")), kind="text-message"
        ).values("evidence_id")
    )
    return (
        session_findings(case, pair_ids)
        .filter(~Exists(references.exclude(evidence_id__in=pair_ids)))
        .annotate(
            member_key=ArraySubquery(
                references.order_by("evidence_id").values("evidence_id")
            ),
            first_at=Subquery(
                member_times.order_by(F("occurred_at").asc(nulls_last=True)).values(
                    "occurred_at"
                )[:1]
            ),
            last_at=Subquery(
                member_times.order_by(F("occurred_at").desc(nulls_last=True)).values(
                    "occurred_at"
                )[:1]
            ),
            parameters=F("result__run__parameters"),
        )
        .order_by(
            F("first_at").asc(nulls_last=True),
            "member_key",
            "-result__run__created_at",
            "-result__run_id",
            "item_index",
            "pk",
        )
    )


def _validated_instances(case, candidates):
    previous = None
    for batch in batched(candidates.iterator(chunk_size=50), 50, strict=False):
        pending = [finding for finding in batch if finding.member_key != previous]
        if not pending:
            continue
        for finding, row in zip(pending, finding_rows(case, pending), strict=True):
            if finding.member_key == previous:
                continue
            members = ordered_members(finding, row)
            if not members or len(members) != len(set(members)):
                continue
            # The latest valid finding represents this exact set of originals.
            previous = finding.member_key
            yield {
                "id": finding.pk,
                "member_ids": tuple(members),
                "source_ids": tuple(finding.source_order),
                "parameters": finding.parameters,
                "url": row["url"],
                "first_at": finding.first_at,
                "last_at": finding.last_at,
                "count": len(members),
                "partial": row["partial"],
                "messages": None,
                "summary": None,
            }


def _valid_ids(values, allowed):
    return (
        isinstance(values, list)
        and values
        and all(isinstance(key, str) for key in values)
        and len(set(values)) == len(values)
        and set(values) <= allowed
    )


def _report_sessions(case, run):
    try:
        sources = resolve_inputs(
            run, case, cache=SourceResolutionCache(case, with_content=False)
        )
    except AnalysisError, Http404:
        return
    by_id = {
        str(source.id): source for source in sources if source.kind == "text-message"
    }
    if any(source.derived_result_id is not None for source in by_id.values()) or len(
        {source.item.pk for source in by_id.values()}
    ) != len(by_id):
        return
    names = participant_names(by_id.values())
    sessions = {}
    source_chunks = {}
    if not isinstance(run.report_items, list):
        return
    for index, item in enumerate(run.report_items):
        if not isinstance(item, dict) or not isinstance(item.get("values"), dict):
            return
        values = item["values"]
        session = values.get("inferred_session")
        if type(session) is not int or session < 1:
            return
        chunk = values.get("chunk_source_ids")
        citations = item.get("source_ids")
        text = item.get("text")
        if (
            not _valid_ids(chunk, by_id.keys())
            or not _valid_ids(citations, set(chunk))
            or "status" not in values
            or not isinstance(text, str)
            or not text.strip()
        ):
            return
        partition = (session, tuple(chunk))
        for key in chunk:
            if source_chunks.setdefault(key, partition) != partition:
                return
        members, statements = sessions.setdefault(session, (set(), []))
        members.update(by_id[key].item.pk for key in chunk)
        if len(statements) < SUMMARY_STATEMENTS:
            statements.append((index, text.strip()))
    # A chunk can produce no statement. Its run-local session is then unknown:
    # a smaller group that matches the remaining chunks cannot claim full coverage.
    if source_chunks.keys() != by_id.keys():
        return
    for members, statements in sessions.values():
        rows = [
            {
                "text": participant_text(text, names),
                "url": finding_url(case.pk, run.pk, index),
            }
            for index, text in statements
        ]
        yield (
            frozenset(members),
            {
                "statements": rows,
                "url": rows[0]["url"],
                "partial": run.status == AnalysisRun.Status.PARTIAL,
            },
        )


def _attach_summaries(case, instances):
    missing = {frozenset(instance["member_ids"]): instance for instance in instances}
    if not missing:
        return
    # Every candidate report must at least include the first original of one
    # visible group. Only completed, current versions can supply a suggestion.
    contains = Q(pk__in=[])
    for instance in instances:
        contains |= Q(
            inputs__contains=[
                {"kind": "text-message", "evidence_id": str(instance["member_ids"][0])}
            ]
        )
    latest = (
        AnalysisRun.objects.filter(
            case=case,
            operation="session_summaries",
            result__case=case,
            status__in=SUCCESSFUL,
        )
        .filter(contains, Q(previous_run__isnull=True) | Q(previous_run__case=case))
        .order_by("equivalence_key", "-created_at", "-pk")
        .distinct("equivalence_key")
    )
    reports = (
        AnalysisRun.objects.filter(pk__in=Subquery(latest.values("pk")))
        .exclude(status=AnalysisRun.Status.EMPTY)
        .annotate(report_items=F("result__data__items"))
        .order_by("-created_at", "-pk")
    )
    for run in reports.iterator(chunk_size=1):
        for membership, summary in _report_sessions(case, run):
            instance = missing.pop(membership, None)
            if instance is not None:
                instance["summary"] = summary
        if not missing:
            break


def get_session_instance(case, messages, instance_id):
    """Resolve a current representative for this exact case/participant pair."""
    target = _instance_id(instance_id)
    candidates = _candidates(case, messages)
    membership = (
        candidates.filter(pk=target).values_list("member_key", flat=True).first()
    )
    if membership:
        instance = next(
            _validated_instances(case, candidates.filter(member_key=membership)), None
        )
        if instance is not None and instance["id"] == target:
            _attach_summaries(case, [instance])
            return instance
    raise Http404("Instance unavailable. Reopen the conversation and try again.")


def _expanded_messages(case, messages, members, page_number):
    page = Paginator(members, MESSAGES_PER_PAGE).get_page(page_number)
    records = messages.select_related("sender", "recipient").in_bulk(page.object_list)
    contexts = {
        row.message_id: row
        for row in imported_contexts(case)
        .filter(message_id__in=records)
        .select_related("source_document")
    }
    if len(records) != len(page.object_list):
        raise Http404("Messages changed. Reopen the conversation and try again.")
    for message in records.values():
        message.imported_source = contexts.get(message.pk)
    page.object_list = [records[key] for key in page.object_list]
    return page


def instance_page(
    case, messages, page_number=None, *, instance_id=None, message_page=None
):
    """Page in SQL, then validate only findings that can represent a visible group."""
    target = _instance_id(instance_id) if instance_id is not None else None
    candidates = _candidates(case, messages)
    keys = (
        candidates.order_by(F("first_at").asc(nulls_last=True), "member_key")
        .values("first_at", "member_key")
        .distinct()
    )
    paginator = Paginator(keys, INSTANCES_PER_PAGE)
    page = paginator.get_page(page_number)
    visible = [tuple(row["member_key"]) for row in page.object_list]
    if target:
        target_key = (
            candidates.filter(pk=target).values("first_at", "member_key").first()
        )
        if target_key is None:
            raise Http404(
                "Instance unavailable. Reopen the conversation and try again."
            )
        if tuple(target_key["member_key"]) not in visible:
            start = target_key["first_at"]
            earlier = (
                Q(first_at__isnull=False) if start is None else Q(first_at__lt=start)
            )
            earlier |= Q(first_at=start, member_key__lt=target_key["member_key"])
            page = paginator.get_page(
                keys.filter(earlier).count() // INSTANCES_PER_PAGE + 1
            )
            visible = [tuple(row["member_key"]) for row in page.object_list]

    rows = {
        tuple(sorted(row["member_ids"])): row
        for row in _validated_instances(case, candidates.filter(member_key__in=visible))
    }
    _attach_summaries(case, rows.values())
    page.object_list = [rows.get(key, {"unavailable": True}) for key in visible]
    if target:
        selected = next((row for row in rows.values() if row["id"] == target), None)
        if selected is None:
            raise Http404(
                "Instance unavailable. Reopen the conversation and try again."
            )
        selected["messages"] = _expanded_messages(
            case, messages, selected["member_ids"], message_page
        )
    return page
