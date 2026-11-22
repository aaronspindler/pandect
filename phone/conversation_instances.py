"""Browse exact, source-validated inferred sessions and their saved summaries."""

from itertools import batched
from uuid import UUID

from django.contrib.postgres.expressions import ArraySubquery
from django.core.paginator import Paginator
from django.db.models import Exists, F, OuterRef, Q, Subquery
from django.http import Http404
from django.urls import reverse

from core.analysis.catalogue import SUCCESSFUL, finding_rows
from core.analysis.errors import AnalysisError
from core.analysis.navigation import finding_url
from core.analysis.presentation import participant_names, participant_text
from core.analysis.services import SourceResolutionCache, resolve_inputs
from core.evidence.queries import scoped_evidence
from core.evidence.source_clocks import date_only_messages
from core.models import AnalysisFindingSource, AnalysisRun
from phone.conversation_sessions import ordered_members, session_findings
from phone.conversations import imported_contexts, pair_messages

INSTANCES_PER_PAGE = 25
MESSAGES_PER_PAGE = 50
INLINE_SUMMARY_CHARACTERS = 8_000


def _instance_id(value):
    try:
        return UUID(str(value))
    except ValueError, TypeError, AttributeError:
        raise Http404("Instance not found.") from None


def _candidates(case, messages, *, touching=None):
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
    first_times = (
        member_times if touching is None else member_times.filter(pk__in=touching)
    )
    return (
        session_findings(case, pair_ids if touching is None else touching)
        .filter(~Exists(references.exclude(evidence_id__in=pair_ids)))
        .annotate(
            member_key=ArraySubquery(
                references.order_by("evidence_id").values("evidence_id")
            ),
            first_at=Subquery(
                first_times.order_by(F("occurred_at").asc(nulls_last=True)).values(
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


def instances_touching_messages(case, message_ids):
    """Valid pair instances ordered by their first time among the given originals."""
    messages = scoped_evidence(case, "text-message").exclude(
        pk__in=date_only_messages(case)
    )
    candidates = _candidates(case, messages, touching=message_ids)
    instances = _validated_instances(case, candidates)
    for batch in batched(instances, INSTANCES_PER_PAGE, strict=False):
        member_ids = {pk for group in batch for pk in group["member_ids"]}
        originals = messages.only(
            "sender_id", "recipient_id", "occurred_at", "is_archived"
        ).in_bulk(member_ids)
        for group in batch:
            members = [originals[pk] for pk in group["member_ids"] if pk in originals]
            pairs = {frozenset((m.sender_id, m.recipient_id)) for m in members}
            if len(members) == group["count"] and len(pairs) == 1:
                yield group, members


def _valid_ids(values, allowed):
    return (
        isinstance(values, list)
        and values
        and all(isinstance(key, str) for key in values)
        and len(set(values)) == len(values)
        and set(values) <= allowed
    )


def _covered_sessions(items, by_id):
    if any(source.derived_result_id is not None for source in by_id.values()) or len(
        {source.item.pk for source in by_id.values()}
    ) != len(by_id):
        return {}
    sessions = {}
    source_chunks = {}
    if not isinstance(items, list):
        return {}
    for index, item in enumerate(items):
        if not isinstance(item, dict) or not isinstance(item.get("values"), dict):
            return {}
        values = item["values"]
        session = values.get("inferred_session")
        if type(session) is not int or session < 1:
            return {}
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
            return {}
        partition = (session, tuple(chunk))
        for key in chunk:
            if source_chunks.setdefault(key, partition) != partition:
                return {}
        members, statements = sessions.setdefault(session, (set(), []))
        members.update(chunk)
        statements.append((index, text.strip()))
    # A chunk can produce no statement. Its run-local session is then unknown:
    # a smaller group that matches the remaining chunks cannot claim full coverage.
    if source_chunks.keys() != by_id.keys():
        return {}
    return sessions


def _report_sessions(
    case, run, memberships, *, full_summary_id, include_statements, preview_only
):
    try:
        sources = resolve_inputs(
            run, case, cache=SourceResolutionCache(case, with_content=False)
        )
    except AnalysisError, Http404:
        return
    by_id = {
        str(source.id): source for source in sources if source.kind == "text-message"
    }
    names = participant_names(by_id.values()) if include_statements else {}
    for members, statements in _covered_sessions(run.report_items, by_id).values():
        membership = frozenset(by_id[key].item.pk for key in members)
        if membership not in memberships:
            continue
        if not include_statements:
            yield membership, None
            continue
        expanded = str(memberships[membership]["id"]) == full_summary_id
        displayed = []
        characters = 0
        for index, text in statements:
            if len(displayed) >= 2 and (
                preview_only
                or (not expanded and characters > INLINE_SUMMARY_CHARACTERS)
            ):
                break
            formatted = participant_text(text, names, include_sender_role=False)
            displayed.append((index, formatted))
            characters += len(formatted)
        summary = {
            "preview": " ".join(text for _, text in displayed[:2]),
            "partial": run.status == AnalysisRun.Status.PARTIAL,
        }
        if preview_only:
            yield membership, summary
            continue
        yield (
            membership,
            summary
            | {
                "expanded": expanded,
                "statements": [
                    {"text": text, "url": finding_url(case.pk, run.pk, index)}
                    for index, text in displayed
                ]
                if expanded or characters <= INLINE_SUMMARY_CHARACTERS
                else [],
                "report_url": reverse("core:analysis:detail", args=(case.pk, run.pk)),
            },
        )


def attach_instance_summaries(
    case,
    instances,
    *,
    full_summary_id=None,
    include_statements=True,
    preview_only=False,
    executions=None,
):
    missing = {frozenset(instance["member_ids"]): instance for instance in instances}
    if not missing:
        return
    executions = executions or {}
    for instance in missing.values():
        instance["summary_routes"] = set()
        instance["summary_missing"] = True
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
        retained = run.parameters.get("_execution")
        route = next(
            (
                backend
                for backend, execution in executions.items()
                if retained == execution
            ),
            None,
        )
        needs_preview = any(
            instance["summary"] is None for instance in missing.values()
        )
        if route is None and not needs_preview:
            continue
        for membership, summary in _report_sessions(
            case,
            run,
            missing,
            full_summary_id=full_summary_id,
            include_statements=include_statements and needs_preview,
            preview_only=preview_only,
        ):
            instance = missing[membership]
            if instance["summary"] is None:
                instance["summary"] = True if summary is None else summary
            if route is not None:
                instance["summary_routes"].add(route)
            if instance["summary_routes"] >= executions.keys():
                instance["summary_missing"] = False
                missing.pop(membership)
        if not missing:
            break


def instance_url(case, pair, instance_id):
    return reverse(
        "phone:conversation_detail",
        args=(case.pk, *sorted(pair)),
        query={"tab": "instances", "instance": instance_id},
        fragment=f"instance-{instance_id}",
    )


def result_instance_url(case, run, item_index, result_items, sources):
    """Locate the current instance for a finding with already resolved inputs."""
    if (
        run.case_id != case.pk
        or run.operation not in {"sessions", "session_summaries"}
        or run.status not in SUCCESSFUL
        or not 0 <= item_index < len(result_items)
    ):
        return ""
    item = result_items[item_index]
    by_id = {
        str(source.id): source for source in sources if source.kind == "text-message"
    }
    if run.operation == "sessions":
        keys = item.get("source_ids")
        if not _valid_ids(keys, by_id.keys()):
            return ""
    else:
        sessions = _covered_sessions(result_items, by_id)
        if not sessions:
            return ""
        keys, _ = sessions[item["values"]["inferred_session"]]
    members = [by_id[key] for key in keys]
    if any(source.derived_result_id is not None for source in members):
        return ""
    pairs = {
        tuple(sorted((source.item.sender_id, source.item.recipient_id)))
        for source in members
    }
    if len(pairs) != 1:
        return ""
    first, second = pairs.pop()
    membership = sorted({source.item.pk for source in members})
    if len(membership) != len(members):
        return ""
    candidates = _candidates(case, pair_messages(case, first, second))
    instance = next(
        _validated_instances(case, candidates.filter(member_key=membership)), None
    )
    if instance is None:
        return ""
    return instance_url(case, (first, second), instance["id"])


def get_session_instance(
    case, messages, instance_id, *, include_statements=True, executions=None
):
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
            attach_instance_summaries(
                case,
                [instance],
                include_statements=include_statements,
                executions=executions,
            )
            return instance
    raise Http404("Instance unavailable. Reopen the conversation and try again.")


def _instance_keys(candidates):
    return (
        candidates.order_by(F("first_at").asc(nulls_last=True), "member_key")
        .values("first_at", "member_key")
        .distinct()
    )


def _page_instance_rows(case, candidates, keys):
    return {
        tuple(sorted(row["member_ids"])): row
        for row in _validated_instances(case, candidates.filter(member_key__in=keys))
    }


def review_instance_page(case, messages, number, per_page, *, executions=None):
    """Validate only the saved membership keys in one bulk review page."""
    candidates = _candidates(case, messages)
    page = Paginator(_instance_keys(candidates), per_page).get_page(number)
    keys = [tuple(row["member_key"]) for row in page.object_list]
    rows = _page_instance_rows(case, candidates, keys)
    attach_instance_summaries(
        case, rows.values(), include_statements=False, executions=executions
    )
    page.object_list = [rows[key] for key in keys if key in rows]
    return page


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
    case,
    messages,
    page_number=None,
    *,
    instance_id=None,
    message_page=None,
    day=None,
    full_summary_id=None,
    summary_executions=None,
):
    """Page in SQL, then validate only findings that can represent a visible group."""
    target = _instance_id(instance_id) if instance_id is not None else None
    candidates = _candidates(case, messages)
    day_instance = None
    if day is not None and target is None:
        dated_messages = messages.filter(occurred_at__date=day).order_by()
        dated_sources = AnalysisFindingSource.objects.filter(
            case=case,
            finding_id=OuterRef("pk"),
            kind="text-message",
            evidence_id__in=dated_messages.values("pk"),
        )
        day_instance = next(
            _validated_instances(case, candidates.filter(Exists(dated_sources))), None
        )
        if day_instance is not None:
            target = day_instance["id"]
    keys = _instance_keys(candidates)
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

    rows = _page_instance_rows(case, candidates, visible)
    attach_instance_summaries(
        case,
        rows.values(),
        full_summary_id=full_summary_id,
        executions=summary_executions,
    )
    page.object_list = [rows.get(key, {"unavailable": True}) for key in visible]
    if target:
        selected = next((row for row in rows.values() if row["id"] == target), None)
        if selected is None:
            raise Http404(
                "Instance unavailable. Reopen the conversation and try again."
            )
        if day_instance is not None:
            dated_ids = set(
                dated_messages.filter(pk__in=selected["member_ids"]).values_list(
                    "pk", flat=True
                )
            )
            for index, member_id in enumerate(selected["member_ids"]):
                if member_id in dated_ids:
                    selected["day_message"] = member_id
                    message_page = index // MESSAGES_PER_PAGE + 1
                    break
        selected["messages"] = _expanded_messages(
            case, messages, selected["member_ids"], message_page
        )
    return page
