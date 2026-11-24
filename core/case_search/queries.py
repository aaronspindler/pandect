"""Federated case search. PostgreSQL orders and pages the narrow candidate rows."""

from uuid import UUID

from django.contrib.postgres.lookups import TrigramSimilar
from django.contrib.postgres.search import TrigramSimilarity
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Case, F, Q, TextField, Value, When, Window
from django.db.models.functions import RowNumber, Upper
from django.urls import reverse

from core.evidence.search import CALL_PARENTS, content_excerpt
from core.evidence.types import CALL_KINDS, COMMUNICATION_KINDS, KINDS
from geo.trip_models import VehicleTripSource
from people.models import Person
from phone.conversations import imported_contexts
from phone.models import CallEvidenceAttachment
from socialmedia.models import SocialAccount
from vehicle.models import Vehicle

from .documents import FIELDS, condition, searchable, term_condition
from .grammar import highlight_terms, parse_query
from .retained import RetainedMatches
from .sources import LABELS, dated, name, sources, vehicle_name

__all__ = ["highlight_terms", "parse_query", "search", "suggest"]


def any_field(fields, lookup, text):
    result = Q(pk__in=[])
    for field in fields:
        result |= Q(**{f"{field}__{lookup}": text})
    return result


def id_match(value):
    try:
        return Q(pk=UUID(value))
    except ValueError:
        return Q(pk__in=[])


def trusted_contexts(case):
    return imported_contexts(case).filter(
        Q(import_run__isnull=True) | Q(import_run__case=case),
        source_sha256=F("source_document__sha256"),
        source_document__is_archived=False,
    )


class RelatedMatches:
    """Reuse related-record lookups across evidence types and query clauses."""

    def __init__(self, case, passages):
        self.case = case
        self.passages = passages
        self.lookups = {}

    def ids(self, category, term):
        key = (category, term)
        if key in self.lookups:
            return self.lookups[key]
        column = "pk"
        if category in CALL_PARENTS:
            column = CALL_PARENTS[category] + "_id"
            rows = CallEvidenceAttachment.objects.filter(
                case=self.case, **{CALL_PARENTS[category] + "__case": self.case}
            )
            rows = searchable(rows, FIELDS["attachment"]).filter(term_condition(term))
        elif category == "context":
            column = "message_id"
            rows = searchable(trusted_contexts(self.case), FIELDS["context"]).filter(
                term_condition(term)
            )
        elif category == "account":
            column = "person_id"
            rows = searchable(
                SocialAccount.objects.filter(person__case=self.case), FIELDS["account"]
            ).filter(term_condition(term))
        elif category == "person":
            rows = (
                Person.objects.filter(case=self.case)
                .annotate(display=name())
                .filter(
                    any_field(
                        ("display", "email", "phone_number"), "icontains", term.text
                    )
                )
            )
        else:
            rows = (
                Vehicle.objects.filter(case=self.case)
                .annotate(display=vehicle_name())
                .filter(
                    any_field(
                        ("display", "vin", "license_plate"), "icontains", term.text
                    )
                )
            )
        rows = rows.values(column).order_by()
        # Empty subqueries in OR predicates can force a sequential scan over
        # a large message export even when the native full-text index is selective.
        self.lookups[key] = rows if rows.exists() else ()
        return self.lookups[key]

    def condition(self, term, kind):
        clause = term_condition(term) if kind != "conversation" else Q(pk__in=[])
        if term.quoted and " " in term.text and kind != "conversation":
            clause &= any_field((*FIELDS[kind], "hit_title"), "icontains", term.text)
        clause |= id_match(term.text)
        if kind in KINDS:
            clause |= Q(pk__in=self.passages.ids(kind, term))
        if kind in CALL_KINDS:
            clause |= Q(pk__in=self.ids(kind, term))
        if kind in ("text-message", "conversation"):
            clause |= Q(pk__in=self.ids("context", term))
        if kind in (*COMMUNICATION_KINDS, "conversation"):
            people = self.ids("person", term)
            clause |= Q(sender_id__in=people) | Q(recipient_id__in=people)
        if kind in ("person-location", "poi"):
            clause |= Q(person_id__in=self.ids("person", term))
        if kind in ("vehicle-location", "trip"):
            clause |= Q(vehicle_id__in=self.ids("vehicle", term))
        if kind == "vehicle-location":
            clause |= Q(raw_row__Asset__icontains=term.text) | Q(
                **{"raw_row__Serial Number__icontains": term.text}
            )
        if kind == "trip":
            points = VehicleTripSource.objects.filter(
                case=self.case,
                input__case=self.case,
                trip__case=self.case,
                input__generation_id=F("trip__generation_id"),
                input__observation__case=self.case,
                input__observation__vehicle_id=F("trip__vehicle_id"),
                input__snapshot__address__icontains=term.text,
            )
            clause |= Q(pk__in=points.values("trip_id")) | Q(
                hit_title__icontains=term.text
            )
        if kind == "person":
            clause |= Q(pk__in=self.ids("account", term))
        if (
            kind == "vehicle"
            and term.text.isascii()
            and term.text.isdecimal()
            and len(term.text) <= 4
        ):
            clause |= Q(year=int(term.text))
        return clause


def rank_rows(qs, source, kind, phrases):
    exact = Q(pk__in=[])
    named = Q(pk__in=[])
    prefix = Q(pk__in=[])
    mentioned = Q(pk__in=[])
    for phrase in phrases:
        exact |= any_field(source.identifiers, "iexact", phrase) | id_match(phrase)
        named |= Q(hit_title__iexact=phrase)
        prefix |= Q(hit_title__istartswith=phrase)
        mentioned |= Q(hit_title__icontains=phrase)
    return (
        qs.annotate(
            hit_kind=Value(kind),
            hit_rank=Case(
                When(exact, then=Value(4)),
                When(named, then=Value(3)),
                When(prefix, then=Value(2)),
                When(mentioned, then=Value(1)),
                default=Value(0),
            ),
        )
        .order_by()
        .values("pk", "hit_kind", "hit_date", "hit_rank")
    )


def combined(queries, sort):
    if not queries:
        return []
    order = {
        "newest": ("-hit_date", "-hit_rank"),
        "oldest": ("hit_date", "-hit_rank"),
    }.get(sort, ("-hit_rank", "-hit_date"))
    return queries[0].union(*queries[1:], all=True).order_by(*order, "hit_kind", "pk")


@transaction.atomic
def search(
    case,
    *,
    query,
    record_type="",
    start=None,
    end=None,
    sort="relevance",
    page=1,
):
    parsed = parse_query(query)
    if not parsed.groups or record_type and record_type not in LABELS:
        return Paginator([], 25).get_page(1)
    types = [record_type] if record_type else LABELS
    retained = RetainedMatches(parsed)
    if not record_type or record_type in KINDS:
        retained.index_passages(case, record_type, start, end)
    related = RelatedMatches(case, retained)
    queries = []
    phrases = [
        " ".join(t.text for t in group if not t.negated) for group in parsed.groups
    ]
    for kind in types:
        source = sources(case, kind)
        qs = dated(source, start, end, kind)
        if kind != "conversation":
            qs = searchable(qs, FIELDS[kind])
        qs = qs.filter(
            condition(
                parsed.groups, lambda term, kind=kind: related.condition(term, kind)
            )
        )
        if kind == "conversation":
            # One conversation per ordered participant pair. Select a real
            # representative message to retain the canonical conversation jump.
            ids = (
                qs.annotate(
                    pair_position=Window(
                        RowNumber(),
                        partition_by=[F("pair_a"), F("pair_b")],
                        order_by=[F("hit_date").desc(), F("pk").asc()],
                    )
                )
                .filter(pair_position=1)
                .order_by()
                .values("pk")
            )
            qs = dated(source, start, end, kind).filter(pk__in=ids)
        elif kind == "finding":
            retained.index_findings(case, qs)
            qs = qs.filter(pk__in=retained.ids("finding"))
        queries.append(rank_rows(qs, source, kind, phrases))
    result = Paginator(combined(queries, sort), 25).get_page(page)
    candidates = list(result.object_list)
    passages, findings = retained.page(case, candidates, parsed)
    result.object_list = hydrate(case, candidates, parsed, passages, findings)
    retained.close()
    return result


def matched_excerpt(kind, parsed):
    # Return the particular field that matched. Raw concatenated search documents
    # also contain checksums and empty fields which are not a readable excerpt.
    fields = [
        field
        for field in FIELDS[kind]
        if field
        not in ("title", "first_name", "last_name", "make", "model", "custom_label")
    ]
    if "body" in fields:
        fields.remove("body")
        fields.insert(0, "body")
    choices = [
        When(**{field + "__icontains": term}, then=content_excerpt(field, term))
        for term in parsed.terms
        for field in fields
    ]
    return Case(*choices, default=Value(""), output_field=TextField())


def hydrate(case, candidates, parsed, passages, findings, *, quick=False):
    rows = {}
    for kind in dict.fromkeys(r["hit_kind"] for r in candidates):
        source = sources(case, kind)
        ids = [r["pk"] for r in candidates if r["hit_kind"] == kind]
        qs = dated(source, None, None, kind).filter(pk__in=ids)
        fields = ["pk", "hit_title", "hit_date", "hit_date_label"]
        if kind in ("text-message", "conversation"):
            fields.extend(("sender_id", "recipient_id"))
        if not quick and kind not in ("conversation", "finding"):
            qs = qs.annotate(hit_excerpt=matched_excerpt(kind, parsed))
            fields.append("hit_excerpt")
        need_related = []
        for obj in qs.values(*fields):
            key = obj["pk"]
            row = {
                "id": key,
                "kind": kind,
                "label": LABELS[kind],
                "title": obj["hit_title"],
                "date": obj["hit_date"],
                "date_label": obj["hit_date_label"],
                "badges": [],
                "terms": parsed.terms,
                "excerpt": obj.get("hit_excerpt", ""),
            }
            if kind in ("text-message", "conversation"):
                first, second = sorted((obj["sender_id"], obj["recipient_id"]))
                row["url"] = (
                    reverse("phone:conversation_detail", args=(case.pk, first, second))
                    + f"?message={key}#message-{key}"
                )
            elif kind in KINDS:
                row["url"] = reverse("core:evidence:detail", args=(case.pk, kind, key))
            elif kind == "finding":
                if key not in findings:
                    continue
                row.update(findings[key])
            else:
                route = {
                    "person": "people:detail",
                    "vehicle": "vehicle:detail",
                    "trip": "geo:trip_detail",
                    "poi": "geo:poi_detail",
                }[kind]
                row["url"] = reverse(route, args=(case.pk, key))
            # Choose one source for the excerpt, destination and attribution.
            if not row["excerpt"]:
                for term in parsed.terms:
                    if match := passages.get((term, kind), {}).get(key):
                        row.update(
                            url=match["url"],
                            excerpt=match["excerpt"],
                            badges=row["badges"] + match["badges"],
                        )
                        break
                else:
                    need_related.append(key)
            row["excerpt"] = row["excerpt"].strip()
            if row["title"].strip().casefold() == row["excerpt"].casefold():
                row["excerpt"] = ""
            rows[(kind, key)] = row
        if not quick and need_related:
            apply_related_excerpt(case, kind, need_related, parsed, rows)
    return [
        rows[(r["hit_kind"], r["pk"])]
        for r in candidates
        if (r["hit_kind"], r["pk"]) in rows
    ]


def apply_related_excerpt(case, kind, ids, parsed, rows):
    if kind in CALL_KINDS:
        parent = CALL_PARENTS[kind] + "_id"
        qs = CallEvidenceAttachment.objects.filter(
            case=case, **{CALL_PARENTS[kind] + "__case": case}
        )
        source_kind = "attachment"
    elif kind == "person":
        parent = "person_id"
        qs = SocialAccount.objects.filter(person__case=case)
        source_kind = "account"
    elif kind in ("text-message", "conversation"):
        parent = "message_id"
        qs = trusted_contexts(case)
        source_kind = "context"
    else:
        return
    qs = searchable(qs.filter(**{parent + "__in": ids}), FIELDS[source_kind])
    positive = Q(pk__in=[])
    for group in parsed.groups:
        for term in group:
            if not term.negated:
                positive |= term_condition(term)
    hits = (
        qs.filter(positive)
        .annotate(snippet=matched_excerpt(source_kind, parsed))
        .order_by(parent, "created_at", "pk")
        .distinct(parent)
        .values(parent, "pk", "snippet")
    )
    for hit in hits:
        key = hit[parent]
        if (kind, key) not in rows or not hit["snippet"]:
            continue
        row = rows[(kind, key)]
        row["excerpt"] = hit["snippet"].strip()
        if source_kind == "attachment":
            row["url"] = (
                reverse("core:evidence:detail", args=(case.pk, kind, key))
                + f"#attachment-{hit['pk']}"
            )
            row["badges"].append("Supplied call material")
        elif source_kind == "account":
            row["url"] += "#social-accounts"
            row["badges"].append("Reported social account")


def suggest(case, query, *, spelling=True):
    parsed = parse_query(query)
    if len(query.strip()) < 2 or len(parsed.groups) != 1 or parsed.exclusions:
        return [], ""
    phrase = " ".join(parsed.terms)
    queries = []
    for kind in (*KINDS, "person", "vehicle", "poi"):
        source = sources(case, kind)
        qs = dated(source, None, None, kind)
        predicate = any_field(
            ("hit_title", *source.identifiers), "istartswith", phrase
        ) | id_match(phrase)
        if kind == "person":
            users = SocialAccount.objects.filter(
                person__case=case, username__istartswith=phrase
            )
            predicate |= Q(pk__in=users.values("person_id"))
        queries.append(rank_rows(qs.filter(predicate), source, kind, (phrase,)))
    candidates = list(combined(queries, "relevance")[:6])
    if candidates:
        return hydrate(case, candidates, parsed, {}, {}, quick=True), ""
    if not spelling or len(phrase) < 3 or any(r.literal for r in parsed.groups[0]):
        return [], ""
    alternatives = []
    for kind in (*KINDS, "person", "vehicle", "poi"):
        source = sources(case, kind)
        qs = dated(source, None, None, kind).alias(upper_title=Upper("hit_title"))
        alternatives.append(
            qs.filter(TrigramSimilar(F("upper_title"), Value(phrase.upper())))
            .annotate(
                similarity=TrigramSimilarity("upper_title", phrase.upper()),
            )
            .filter(similarity__gte=0.38)
            .order_by()
            .values("hit_title", "similarity")
        )
    best = (
        alternatives[0]
        .union(*alternatives[1:], all=True)
        .order_by("-similarity", "hit_title")[:1]
    )
    return [], next((r["hit_title"] for r in best), "")
