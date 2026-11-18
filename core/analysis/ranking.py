"""Rank a scoped findings list before selecting previews or page boundaries."""

from collections import defaultdict
from itertools import batched

from django.db.models import (
    Case,
    CharField,
    Count,
    Exists,
    F,
    OuterRef,
    Q,
    Value,
    When,
    Window,
)
from django.db.models.functions import Cast, Coalesce, Concat, NullIf, RowNumber

from core.analysis.estimate_dependencies import verified_estimate_uses
from core.analysis.review_signals import REVIEW_SIGNALS
from core.models import AnalysisFinding, AnalysisFindingSource

MAX_MATCH_KEYS = 64
MAX_MATCH_ORIGINALS = 5

REVIEW_ORDER = (
    "repeat_position",
    "review_priority",
    "-review_created_at",
    "-review_run_id",
    "item_index",
    "pk",
)


def _verified_matches(queryset, case):
    # Files can disappear and source snapshots can change. Count only keys with
    # two different, currently readable originals and distinct content hashes.
    from core.analysis.catalogue import finding_queryset, finding_rows

    digests = queryset.exclude(match_digest="").order_by().values("match_digest")
    current = finding_queryset(case, {}).filter(match_digest__in=digests)
    references = AnalysisFindingSource.objects.filter(case=case, finding__in=current)
    key_fields = (
        "finding__match_kind",
        "finding__match_version",
        "finding__match_digest",
    )
    # A call addition is an original in its own right. Bound actual original
    # identities so even a large collection of byte copies cannot expand a read.
    original = Concat(
        "kind",
        Value(":"),
        Cast("evidence_id", CharField()),
        Value(":"),
        Coalesce(Cast("attachment_id", CharField()), Value("")),
    )
    groups = list(
        references.order_by()
        .values(*key_fields)
        .annotate(
            content_count=Count("source_sha256", distinct=True),
            original_count=Count(original, distinct=True),
        )
        .filter(content_count__gte=2, original_count__lte=MAX_MATCH_ORIGINALS)
        .order_by("original_count", *key_fields)
        .values_list(*key_fields)[:MAX_MATCH_KEYS]
    )
    if not groups:
        return {}
    candidates = Q(pk__in=[])
    for key in groups:
        candidates |= Q(**dict(zip(key_fields, key, strict=True)))
    # Check one occurrence per original, selecting an occurrence in the list
    # when possible. Limit applies to contextual lookups only, never to results.
    fields = (*key_fields, "kind", "evidence_id", "attachment_id")
    samples = (
        references.filter(candidates)
        .alias(in_scope=Exists(queryset.filter(pk=OuterRef("finding_id"))))
        .order_by(
            *fields,
            "-in_scope",
            "-finding__result__run__created_at",
            "-finding__result__run_id",
            "finding__item_index",
            "pk",
        )
        .distinct(*fields)
        .values_list("finding_id", flat=True)
    )
    checked = defaultdict(dict)
    for ids in batched(samples.iterator(chunk_size=128), 128, strict=False):
        findings = list(
            AnalysisFinding.objects.filter(case=case, pk__in=ids)
            .annotate(run_id=F("result__run_id"), excerpt=Value(""))
            .only(
                "pk",
                "case_id",
                "result_id",
                "item_index",
                "label",
                "weak",
                "preview",
                "match_kind",
                "match_version",
                "match_digest",
            )
        )
        for item, row in zip(findings, finding_rows(case, findings), strict=True):
            if row["unavailable"] or any(
                source["archived"] for source in row["sources"]
            ):
                continue
            key = (item.match_kind, item.match_version, item.match_digest)
            checked[key][item.pk] = {source["sha256"] for source in row["sources"]}
    verified = {}
    for (kind, _, digest), occurrences in checked.items():
        if len(set().union(*occurrences.values())) >= 2:
            verified.update(dict.fromkeys(occurrences, (kind, digest)))
    return verified


def review_context(queryset, case):
    """Resolve current context once before building possibly correlated list queries."""
    return _verified_matches(queryset, case), verified_estimate_uses(queryset, case)


def rank_findings(queryset, case, *, context=None):
    matches, estimates = review_context(queryset, case) if context is None else context
    conditions = {signal: Q(review_signal=signal) for signal in REVIEW_SIGNALS}
    conditions["used_estimate"] = Q(pk__in=estimates)
    conditions["matched_identifier"] = Q(pk__in=matches)
    reasons = Case(
        *(
            When(conditions[signal], then=Value(reason))
            for signal, reason in REVIEW_SIGNALS.items()
        ),
        When(
            repeat_count__gt=1,
            then=Value("Other saved findings repeat this observation."),
        ),
        default=Value(""),
    )
    priorities = Case(
        When(weak=True, then=Value(len(REVIEW_SIGNALS) + 1)),
        *(
            When(conditions[signal], then=Value(index))
            for index, signal in enumerate(REVIEW_SIGNALS)
        ),
        default=Value(len(REVIEW_SIGNALS)),
    )
    # Select the most useful representative in each group, then place the first
    # occurrence of every group before revisiting observations already shown.
    group = Coalesce(NullIf("repeat_key", Value("")), Cast("pk", CharField()))
    return (
        queryset.annotate(
            review_priority=priorities,
            review_created_at=F("result__run__created_at"),
            review_run_id=F("result__run_id"),
        )
        .alias(repeat_count=Window(Count("pk"), partition_by=group))
        .annotate(
            repeat_position=Window(
                RowNumber(),
                partition_by=group,
                order_by=[
                    F("review_priority").asc(),
                    F("review_created_at").desc(),
                    F("review_run_id").desc(),
                    F("item_index").asc(),
                    F("pk").asc(),
                ],
            )
        )
        .alias(candidate_reason=Case(When(weak=False, then=reasons), default=Value("")))
        .annotate(
            review_reason=Case(
                When(repeat_position=1, then=F("candidate_reason")), default=Value("")
            )
        )
        .order_by(*REVIEW_ORDER)
    )
