"""Exact shared selection references, source resolution, and portable briefs."""

import json
from dataclasses import replace
from uuid import UUID

from django.core import signing
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import Q
from django.http import Http404
from django.template.loader import render_to_string
from django.utils import timezone

from core.analysis.errors import AnalysisError, error_message
from core.analysis.navigation import finding_url
from core.analysis.projection import is_ai_suggestion
from core.analysis.services import (
    SourceResolutionCache,
    require_current_geocode_reviews,
    resolve_inputs,
    resolve_snapshots,
    snapshot_sources,
)
from core.models import AnalysisResult, AnalysisReview, SavedSelection

MAX_MEMBERS = 500
MAX_BRIEF_BYTES = 16 * 1024 * 1024
EXCERPT_CHARACTERS = 4000
REVISION_SALT = "core.selection.revision"


def revision_token(selection):
    return signing.dumps(
        {"id": str(selection.pk), "revision": selection.revision}, salt=REVISION_SALT
    )


def check_revision(selection, token):
    try:
        valid = signing.loads(token, salt=REVISION_SALT) == {
            "id": str(selection.pk),
            "revision": selection.revision,
        }
    except signing.BadSignature, TypeError:
        valid = False
    if not valid:
        raise ValidationError(
            "This selection changed. Review the latest items and submit again."
        )


def member_key(member):
    return json.dumps(member, sort_keys=True, separators=(",", ":"))


def source_member(case, choice):
    return {"type": "source", "source": snapshot_sources(case, [choice])[0]}


def finding_member(case, result_id, item_index):
    try:
        result_id = str(UUID(str(result_id)))
        if type(item_index) is not int or item_index < 0:
            raise ValueError
    except ValueError, TypeError:
        raise ValidationError("Choose an available retained finding.") from None
    member = {"type": "finding", "result_id": result_id, "item_index": item_index}
    resolve_member(member, case)
    return member


def append_member(selection, member):
    """Caller holds the authorized case/selection lock and checked its revision."""
    if member_key(member) in {member_key(value) for value in selection.members}:
        return False
    if len(selection.members) >= MAX_MEMBERS:
        raise ValidationError("A selection supports at most 500 items.")
    selection.members = [*selection.members, member]
    return True


def review_snapshot(review):
    if review is None:
        return None
    return {
        "id": str(review.pk),
        "decision": review.decision,
        "reason": review.comment,
        "reviewer_id": str(review.reviewed_by_id),
        "reviewer": str(review.reviewed_by),
        "created_at": review.created_at.isoformat(),
        "item_index": review.item_index,
    }


def _source_warning(snapshot, case):
    if not snapshot.get("review_id"):
        return []
    review = AnalysisReview.objects.filter(
        case=case,
        result__case=case,
        result__run__case=case,
        pk=snapshot["review_id"],
    ).first()
    if not review:
        raise AnalysisError("source_unavailable")
    current = AnalysisReview.objects.filter(
        case=case,
        result=review.result,
        item_index=review.item_index,
    ).first()
    if current and current.pk != review.pk:
        return [
            "The retained location approval has a newer review. "
            "This selection preserves its original approval."
        ]
    return []


class SelectionResolver:
    """One request's source checks and selected items from each retained result."""

    def __init__(
        self, case, members, *, require_current_review=False, excerpt_only=False
    ):
        self.case = case
        self.require_current_review = require_current_review
        self.excerpt_only = excerpt_only
        # Finding validation needs source metadata, not every contributing body.
        self.sources = SourceResolutionCache(case, with_content=False)
        self.selected_sources = SourceResolutionCache(case)
        self.results = {}
        self.errors = {}
        self.retained_bytes = 0
        self.indices = {}
        for member in members:
            if isinstance(member, dict) and member.get("type") == "finding":
                index = member.get("item_index")
                if (
                    type(index) is int
                    and index >= 0
                    and isinstance(member.get("result_id"), str)
                ):
                    self.indices.setdefault(member.get("result_id"), set()).add(index)

    def result(self, result_id):
        if result_id in self.results:
            return self.results[result_id]
        if result_id in self.errors:
            raise AnalysisError(self.errors[result_id])
        if self.retained_bytes > MAX_BRIEF_BYTES:
            raise ValidationError(
                "Selected content exceeds 16 MiB. "
                "Split this selection into smaller selections."
            )
        try:
            return self._read_result(result_id)
        except AnalysisError as error:
            self.errors[result_id] = error.code
            raise
        except AnalysisResult.DoesNotExist, ValueError, ValidationError:
            self.errors[result_id] = "source_unavailable"
            raise

    def _read_result(self, result_id):
        result = (
            AnalysisResult.objects.select_related("run")
            .defer("provenance")
            .get(
                pk=result_id,
                case=self.case,
                run__case=self.case,
                run__status__in=("complete", "partial", "empty"),
            )
        )
        sources = resolve_inputs(
            result.run,
            self.case,
            require_current_review=self.require_current_review,
            cache=self.sources,
        )
        indices = self.indices.get(result_id, set())
        items = result.data.get("items", [])
        selected_items, total_characters = {}, {}
        for index in indices:
            if index >= len(items):
                continue
            item = items[index]
            total_characters[index] = len(item.get("text", ""))
            selected_items[index] = (
                {**item, "text": item.get("text", "")[:EXCERPT_CHARACTERS]}
                if self.excerpt_only
                else item
            )
        self.retained_bytes += len(
            json.dumps(selected_items, cls=DjangoJSONEncoder).encode()
        )
        if self.retained_bytes > MAX_BRIEF_BYTES:
            raise ValidationError(
                "Selected content exceeds 16 MiB. "
                "Split this selection into smaller selections."
            )
        warnings = list(result.data.get("warnings", []))
        if result.run.status == "partial":
            warnings.append("Partial analysis coverage.")
        try:
            require_current_geocode_reviews(result.run, sources)
        except AnalysisError:
            warnings.append(
                "A contributing location approval changed after "
                "this result was published."
            )
        reviews = {
            review.item_index: review_snapshot(review)
            for review in result.reviews.filter(case=self.case)
            .filter(Q(item_index__in=indices) | Q(item_index__isnull=True))
            .select_related("reviewed_by")
            .order_by("item_index", "-created_at", "id")
            .distinct("item_index")
        }
        # Cache only selected items, not every unused item in a 4 MiB result.
        result.__dict__.pop("data", None)
        resolved = {
            "result": result,
            "sources": sources,
            "items": selected_items,
            "total_characters": total_characters,
            "warnings": warnings,
            "reviews": reviews,
        }
        self.results[result_id] = resolved
        return resolved


def resolve_member(member, case, *, require_current_review=False, resolver=None):
    if not isinstance(member, dict):
        raise AnalysisError("source_unavailable")
    resolver = resolver or SelectionResolver(
        case, [member], require_current_review=require_current_review
    )
    if resolver.case.pk != case.pk:
        raise AnalysisError("source_unavailable")
    if member.get("type") == "source":
        snapshot = member["source"]
        sources = resolve_snapshots(
            [snapshot],
            case,
            require_current_review=require_current_review,
            cache=resolver.selected_sources,
        )
        source = sources[0]
        return {
            "member": member,
            "type": "source",
            "available": True,
            "title": source.item.title,
            "text": source.text,
            "url": source.url,
            "source": source,
            "sources": sources,
            "snapshots": [snapshot],
            "warnings": _source_warning(snapshot, case),
        }
    if member.get("type") != "finding":
        raise AnalysisError("source_unavailable")
    index = member["item_index"]
    if type(index) is not int or index < 0:
        raise AnalysisError("source_unavailable")
    resolved = resolver.result(member["result_id"])
    if index not in resolved["items"]:
        raise AnalysisError("source_unavailable")
    finding = resolved["items"][index]
    result = resolved["result"]
    cited = [
        source
        for source in resolved["sources"]
        if str(source.id) in finding.get("source_ids", [])
    ]
    return {
        "member": member,
        "type": "finding",
        "available": True,
        "title": finding.get("label", "Finding"),
        "text": finding.get("text", ""),
        "total_characters": resolved["total_characters"][index],
        "url": finding_url(case.pk, result.run_id, index, show_weak=True),
        "finding": finding,
        "ai_suggestion": is_ai_suggestion(result.run.operation, finding),
        "result": result,
        "sources": cited,
        "snapshots": result.run.inputs,
        "review": resolved["reviews"].get(index),
        "result_review": resolved["reviews"].get(None),
        "warnings": resolved["warnings"],
    }


def resolve_selection(
    selection, case, *, require_current_review=False, excerpt_only=False
):
    if selection.case_id != case.pk:
        raise Http404("Selection not found.")
    members, snapshots, unavailable = [], {}, []
    content_bytes = 0
    resolver = SelectionResolver(
        case,
        selection.members,
        require_current_review=require_current_review,
        excerpt_only=excerpt_only,
    )
    for index, member in enumerate(selection.members):
        try:
            row = resolve_member(
                member,
                case,
                require_current_review=require_current_review,
                resolver=resolver,
            )
            row.setdefault("total_characters", len(row["text"]))
            if excerpt_only:
                row["text"] = row["text"][:EXCERPT_CHARACTERS]
                if row.get("finding"):
                    row["finding"] = {**row["finding"], "text": row["text"]}
                row["sources"] = [
                    replace(
                        source,
                        extracted_text=source.extracted_text[:EXCERPT_CHARACTERS],
                    )
                    if source.extracted_text is not None
                    else source
                    for source in row["sources"]
                ]
                if row.get("source"):
                    row["source"] = row["sources"][0]
            content_bytes += len(row["text"].encode()) + len(
                json.dumps(row.get("finding", {}), cls=DjangoJSONEncoder).encode()
            )
            if content_bytes > MAX_BRIEF_BYTES:
                raise ValidationError(
                    "Selected content exceeds 16 MiB. "
                    "Split this selection into smaller selections."
                )
            for snapshot in row["snapshots"]:
                key = member_key(snapshot)
                snapshots.setdefault(key, snapshot)
        except (
            AnalysisError,
            AnalysisResult.DoesNotExist,
            Http404,
            KeyError,
            ValueError,
            TypeError,
            ValidationError,
        ) as error:
            row = {
                "member": member,
                "available": False,
                "title": f"Unavailable item {index + 1}",
                "error": error_message(error.code)
                if isinstance(error, AnalysisError)
                else (
                    " ".join(error.messages)
                    if isinstance(error, ValidationError)
                    else "The retained source or finding is no longer "
                    "available in this case."
                ),
            }
            unavailable.append(row)
        finally:
            # Keep checked identities, but release full extraction/original payloads
            # before resolving another member. Rows retain their needed text only.
            resolver.selected_sources.prior_results.clear()
            for item in (
                *resolver.selected_sources.originals.values(),
                *resolver.selected_sources.attachments.values(),
            ):
                if item is not None:
                    item.__dict__.pop("body", None)
                    item.__dict__.pop("transcript", None)
        row["index"] = index
        if row["available"]:
            row["excerpt"] = row["text"][:EXCERPT_CHARACTERS]
            row["excerpt_end"] = len(row["excerpt"])
            row["truncated"] = row["total_characters"] > EXCERPT_CHARACTERS
        members.append(row)
    return {
        "members": members,
        "snapshots": list(snapshots.values()),
        "unavailable": unavailable,
    }


def selection_choices(resolved):
    if resolved["unavailable"]:
        raise ValidationError(
            "Resolve unavailable selection items before starting analysis."
        )
    if any(row["type"] != "source" for row in resolved["members"]):
        raise ValidationError(
            "This selection includes findings. Use question synthesis, or create "
            "a source-only selection for other analysis."
        )
    return [
        {
            "kind": row["source"].kind,
            "id": str(row["source"].item.pk),
            "attachment_id": row["member"]["source"].get("attachment_id"),
            "result_id": row["member"]["source"].get("result_id"),
            "geocode_candidate": row["member"]["source"].get("geocode_candidate"),
        }
        for row in resolved["members"]
    ]


def _source_manifest(source, snapshots, derivations):
    snapshot = next(value for value in snapshots if value["id"] == str(source.id))
    manifest = {
        "reference": snapshot,
        "title": source.item.title,
        "source": source.item.source,
        "source_url": source.item.source_url,
        "source_description": source.item.source_description,
        "archived": source.item.is_archived,
        "collected_at": source.item.collected_at,
        "created_at": source.item.created_at,
        "anchor": source.anchor(),
    }
    if snapshot.get("result_id"):
        result_id = snapshot["result_id"]
        if result_id not in derivations:
            result = (
                AnalysisResult.objects.select_related("run")
                .defer("data")
                .get(
                    pk=result_id,
                    case=source.item.case_id,
                    run__case=source.item.case_id,
                )
            )
            derivations[result_id] = {
                "result_id": str(result.pk),
                "run_id": str(result.run_id),
                "operation": result.run.operation,
                "method_version": result.run.method_version,
                "parameters": result.run.parameters,
                "provenance": result.provenance,
                "status": result.run.status,
                "result_review": review_snapshot(
                    result.reviews.filter(
                        case_id=source.item.case_id, item_index__isnull=True
                    )
                    .select_related("reviewed_by")
                    .first()
                ),
            }
        manifest["derivation"] = derivations[result_id]
    if snapshot.get("review_id"):
        manifest["retained_approval"] = review_snapshot(
            AnalysisReview.objects.select_related("reviewed_by").get(
                pk=snapshot["review_id"],
                case_id=source.item.case_id,
            )
        )
    return manifest


def build_brief(selection, case):
    resolved = resolve_selection(selection, case, excerpt_only=True)
    if resolved["unavailable"]:
        raise ValidationError(
            [
                f"Item {row['index'] + 1}: {row['error']}"
                for row in resolved["unavailable"]
            ]
        )
    if not resolved["members"]:
        raise ValidationError(
            "Add at least one source or finding before exporting a brief."
        )
    manifest = {
        "schema": "pandect.selection-brief.v1",
        "generated_at": timezone.now().isoformat(),
        "case": {"id": str(case.pk), "name": case.name},
        "selection": {
            "id": str(selection.pk),
            "name": selection.name,
            "revision": selection.revision,
        },
        "members": [],
    }
    manifest_bytes = 0
    derivations = {}
    for row in resolved["members"]:
        entry = {
            "reference": row["member"],
            "title": row["title"],
            "excerpt": {
                "text": row["excerpt"],
                "start": 0,
                "end": row["excerpt_end"],
                "total_characters": row["total_characters"],
                "truncated": row["truncated"],
            },
            "sources": [
                _source_manifest(source, row["snapshots"], derivations)
                for source in row["sources"]
            ],
            "warnings": row["warnings"],
        }
        if row["type"] == "finding":
            result = row["result"]
            entry.update(
                {
                    "anchors": row["finding"].get("anchors", []),
                    "finding_citations": row["finding"].get("finding_citations", []),
                    "values": row["finding"].get("values", {}),
                    "finding_review": row["review"],
                    "ai_suggestion": row["ai_suggestion"],
                    "result_review": row["result_review"],
                    "analysis": {
                        "run_id": str(result.run_id),
                        "result_id": str(result.pk),
                        "operation": result.run.operation,
                        "method_version": result.run.method_version,
                        "parameters": result.run.parameters,
                        "status": result.run.status,
                        "created_at": result.created_at,
                        "provenance": result.provenance,
                        "contributing_sources": row["snapshots"],
                    },
                }
            )
        manifest_bytes += len(json.dumps(entry, cls=DjangoJSONEncoder).encode())
        if manifest_bytes > MAX_BRIEF_BYTES:
            raise ValidationError(
                "This brief exceeds 16 MiB. Export a smaller selection."
            )
        manifest["members"].append(entry)
    html = render_to_string(
        "core/analysis/selection_brief.html",
        {"manifest": manifest, "selection": selection},
    )
    if len(html.encode()) > MAX_BRIEF_BYTES:
        raise ValidationError("This brief exceeds 16 MiB. Export a smaller selection.")
    return html


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
