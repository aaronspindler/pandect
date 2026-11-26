"""Apply verified AI Search results as ID filters on existing record browsers."""

from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class Discovery:
    evidence: dict = field(default_factory=lambda: defaultdict(set))
    findings: dict = field(default_factory=dict)
    records: dict = field(default_factory=lambda: defaultdict(set))
    excerpts: dict = field(default_factory=dict)
    notice: str = ""


def discover(case, actor, query, *, kind="", records=False):
    from .hosted import NOTICES, HostedSearchError, hits

    result = Discovery()
    if not query:
        return result
    if actor is None or not case.external_ai_enabled:
        result.notice = NOTICES["search_disabled"]
        return result
    try:
        passages = hits(
            case, actor, query, limit=50, filters={"record_type": kind}, records=records
        )
    except HostedSearchError as exc:
        result.notice = NOTICES.get(exc.code, NOTICES["search_unavailable"])
        return result
    for material, _, text in passages:
        locator = material.locator
        start = max(0, text.casefold().find(query.casefold()) - 60)
        excerpt = text[start : start + 240]
        if locator["surface"] in ("record", "trip"):
            key = locator["kind"] if locator["surface"] == "record" else "trip"
            result.records[key].add(locator.get("evidence_id") or locator["pk"])
        if locator["surface"] == "finding":
            result.findings.setdefault(locator["pk"], excerpt)
        if locator["surface"] in ("original", "attachment", "passage", "finding"):
            originals = (
                [locator["record_key"]]
                if locator.get("evidence_id")
                else locator.get("cited_originals", ())
            )
        else:
            originals = ()
        for original in originals:
            kind, key = original.split(":", 1)
            result.evidence[kind].add(key)
            result.excerpts.setdefault(
                (kind, key),
                {
                    "excerpt": excerpt,
                    "label": locator["label"],
                    "url": material.url,
                },
            )
    return result
