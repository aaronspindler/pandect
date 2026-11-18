"""Validate retained analysis before it contributes to matches or counts."""

from itertools import batched
from uuid import uuid4

from django.db import connection
from django.db.models import Exists, F, OuterRef, Q
from django.db.models.expressions import RawSQL
from django.http import Http404

from core.analysis.catalogue import finding_queryset, finding_rows
from core.analysis.errors import AnalysisError
from core.analysis.navigation import finding_url
from core.analysis.services import SourceResolutionCache, resolve_inputs
from core.evidence.search import content_excerpt
from core.evidence.types import KINDS
from core.models import ExtractedPassage

from .documents import FIELDS, searchable, term_condition
from .sources import dated, sources


def retained_passages(case, kind="", *, originals=None):
    queryset = ExtractedPassage.objects.filter(
        case=case,
        result__case=case,
        result__run__case=case,
        result__run__status__in=("complete", "partial"),
    )
    if kind:
        if kind not in KINDS:
            return queryset.none()
        queryset = queryset.filter(kind=kind)
    if originals:
        eligible = Q(pk__in=[])
        for candidate in (kind,) if kind else KINDS:
            current = originals(candidate).filter(
                pk=OuterRef("evidence_id"), sha256=OuterRef("evidence_sha256")
            )
            eligible |= Q(kind=candidate) & Exists(current)
        queryset = queryset.filter(eligible)
    return queryset


class RetainedMatches:
    """Request-local, transaction-bound IDs. Large result sets stay in PostgreSQL."""

    def __init__(self, parsed):
        self.terms = tuple(dict.fromkeys(t for group in parsed.groups for t in group))
        self.name = "search_matches_" + uuid4().hex
        self.present = set()

    def store(self, rows):
        if not rows:
            return
        with connection.cursor() as cursor:
            if not self.present:
                cursor.execute(
                    f"CREATE TEMP TABLE {self.name} (kind text, term int, record uuid, "
                    "source uuid, PRIMARY KEY(kind, term, record)) ON COMMIT DROP"
                )
            cursor.executemany(
                f"INSERT INTO pg_temp.{self.name} VALUES (%s, %s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                rows,
            )
        self.present.update((kind, term) for kind, term, _, _ in rows)

    def ids(self, kind, term=None):
        index = -1 if term is None else self.terms.index(term)
        if (kind, index) not in self.present:
            return ()
        return RawSQL(
            f"SELECT record FROM pg_temp.{self.name} WHERE kind = %s AND term = %s",
            (kind, index),
        )

    def index_passages(self, case, kind, state, start, end):
        def dated_originals(candidate):
            return dated(sources(case, candidate, state), start, end, candidate)

        base = retained_passages(
            case, kind, originals=dated_originals if start or end else None
        )
        base = (
            searchable(base, FIELDS["passage"])
            .select_related("result__run")
            .only(
                "kind",
                "evidence_id",
                "evidence_sha256",
                "attachment_id",
                "item_index",
                "result__run__inputs",
                "result__run__operation",
                "result__run__status",
            )
        )
        for index, term in enumerate(self.terms):
            # Prefer recent retained versions when several versions match.
            candidates = base.filter(term_condition(term)).order_by(
                "-result__created_at", "result_id", "pk"
            )
            last_run = None
            for chunk in batched(
                candidates.iterator(chunk_size=100), 100, strict=False
            ):
                matched = []
                for passage in chunk:
                    run = passage.result.run
                    if last_run != run.pk:
                        current_inputs = valid_sources(run, case, state)
                        last_run = run.pk
                    if passage_identity(passage) in current_inputs:
                        matched.append(
                            (passage.kind, index, passage.evidence_id, passage.pk)
                        )
                self.store(matched)

    def index_findings(self, case, candidates, state):
        candidates = finding_values(candidates, F("preview"))
        for chunk in batched(candidates.iterator(chunk_size=100), 100, strict=False):
            rows = finding_rows(case, chunk)
            self.store(
                [
                    ("finding", -1, r["id"], r["id"])
                    for r in rows
                    if visible_finding(r, state)
                ]
            )

    def page(self, case, candidates, state, parsed):
        """Resolve current-page excerpts and links, and retain their version labels."""
        passages = {}
        findings = {}
        if not self.present or not candidates:
            return passages, findings
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT kind, term, record, source FROM pg_temp.{self.name} "
                "WHERE record = ANY(%s) ORDER BY term",
                ([row["pk"] for row in candidates],),
            )
            records = cursor.fetchall()
        finding_ids = [key for kind, index, key, _ in records if kind == "finding"]
        if finding_ids:
            items = finding_values(
                finding_queryset(case, {}).filter(pk__in=finding_ids),
                content_excerpt("search_text", parsed.terms[0]),
            )
            for row in finding_rows(case, items):
                if visible_finding(row, state):
                    findings[row["id"]] = {
                        "title": row["label"],
                        "url": row["url"],
                        "excerpt": row["excerpt"] or row["preview"],
                        "badges": finding_badges(row),
                    }
        for index, term in enumerate(self.terms):
            if term.negated:
                continue
            ids = [source for _, number, _, source in records if number == index]
            if not ids:
                continue
            items = (
                retained_passages(case)
                .filter(pk__in=ids)
                .annotate(excerpt=content_excerpt("text", term.text))
                .select_related("result__run")
                .only(
                    "kind",
                    "evidence_id",
                    "evidence_sha256",
                    "attachment_id",
                    "item_index",
                    "result__run__inputs",
                    "result__run__operation",
                    "result__run__status",
                )
            )
            for passage in items:
                run = passage.result.run
                if passage_identity(passage) not in valid_sources(run, case, state):
                    continue
                passages.setdefault((term.text, passage.kind), {})[
                    passage.evidence_id
                ] = {
                    "url": finding_url(case.pk, run.pk, passage.item_index),
                    "excerpt": passage.excerpt,
                    "badges": ["Extracted content"]
                    + (["Partial coverage"] if run.status == "partial" else []),
                }
        return passages, findings

    def close(self):
        if self.present:
            with connection.cursor() as cursor:
                cursor.execute(f"DROP TABLE pg_temp.{self.name}")


def valid_sources(run, case, state, cache=None):
    try:
        inputs = resolve_inputs(
            run, case, cache=cache or SourceResolutionCache(case, with_content=False)
        )
    except AnalysisError, Http404:
        inputs = ()
    return {
        (
            src.kind,
            src.item.pk,
            src.item.sha256,
            src.attachment.pk if src.attachment else None,
        ): src.item
        for src in inputs
        if state == "all" or src.item.is_archived == (state == "archived")
    }


def passage_identity(passage):
    return (
        passage.kind,
        passage.evidence_id,
        passage.evidence_sha256,
        passage.attachment_id,
    )


def finding_values(qs, excerpt):
    return (
        qs.annotate(run_id=F("result__run_id"), excerpt=excerpt)
        .only("result_id", "item_index", "label", "weak", "preview", "case_id")
        .order_by()
    )


def visible_finding(row, state):
    archived = any(source["archived"] for source in row["sources"])
    return not row["unavailable"] and (
        state == "all" or archived == (state == "archived")
    )


def finding_badges(row):
    badges = []
    if row["uses_ai"]:
        badges.append("AI suggestion")
    if row["partial"]:
        badges.append("Partial coverage")
    if any(source["archived"] for source in row["sources"]):
        badges.append("Archived source")
    return badges + [value for value in (row["weak_label"], row["warning"]) if value]
