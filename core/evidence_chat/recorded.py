"""Verified field queries and reusable message selections for Evidence Chat."""

import re
from datetime import date

from cases.models import CaseActivity
from core.analysis.errors import AnalysisError
from core.evidence.types import KINDS
from core.models import ChatTurn
from people.models import Person
from phone import recorded_messages
from phone.presentation import message_destination

from . import corpus, engine, selections
from .engine_counts import _people, day_bounds
from .history import TURN_NUMBER
from .search_scope import (
    DATE,
    QUOTED_TEXT,
    TYPE,
    normalize_date_filters,
    search_filters,
)

FIELDS = ("recipients", "senders", "first", "last", "count", "weeks", "types")
NAME = r"(?P<party>[^?!,;:“”\"\n]{1,100}?)"
RECIPIENT = re.compile(
    rf"(?:to )?who(?:m)?(?: all)? did {NAME} "
    r"(?:say|send|write|text) QUOTE(?: to)?[?.!]?",
    re.I,
)
RECEIVERS = re.compile(
    r"who(?: all)? (?:received|was sent) "
    r"(?:messages? (?:containing|saying) )?QUOTE[?.!]?",
    re.I,
)
TEXT_PARTIES = re.compile(
    rf"(?:who(?:m)?(?: all)? did {NAME} (?:text|message|contact by text)|"
    r"which (?:people|recipients) received messages from "
    r"(?P<source>[^?!.]{1,100}))[?.!]?",
    re.I,
)
RECORD_TOPIC = re.compile(
    r"\b(?:messages?|texts?|senders?|recipients?|destinations?|sent|texted|received)\b",
    re.I,
)
FOLLOW = re.compile(
    r"(?:which (?:of (?:those|these|them)(?: messages?)? )?"
    r"(?:(?:came|happened|was sent|occurred|arrived) )?(?P<order>first|last)"
    r"|(?:has (?:anything|anything else) (?:changed|turned up)"
    r"|what(?:'s| is) new|any new matches))"
    r"(?: (?:since|in|from) (?:answer|reply|turn|question)\s*#?\d{1,8})?[?.!]?",
    re.I,
)
PRONOUN = {"she", "he", "they", "her", "him", "them"}


def literal(question):
    matches = QUOTED_TEXT.findall(question)
    if len(matches) != 1:
        return ""
    phrase = matches[0][1:-1]
    return phrase if len(phrase) <= 160 and re.search(r"[^\W_]{3}", phrase) else ""


def _scope(turn):
    # Recognize explicit event-date filters in both chat and natural form.
    # Mask quotations so that a date within the searched wording stays literal.
    wording = QUOTED_TEXT.sub(" ", turn.question)
    wording = normalize_date_filters(wording)
    filters = search_filters(wording, turn.plan.get("timezone", "UTC"))
    if any(value.lower() != "text-message" for value in TYPE.findall(wording)):
        return None
    try:
        for mode, raw in DATE.findall(wording):
            if date.fromisoformat(raw) == date.max and mode.lower() in (
                "on",
                "through",
            ):
                return None
    except ValueError:
        return None
    if re.search(r"(?<!\S)(?:on|from|through):", DATE.sub("", wording), re.I):
        return None
    return filters


def context_selection(turn):
    """Only investigator-defined predicates may name a complete population."""
    question = turn.question
    wording = QUOTED_TEXT.sub(" ", question)
    scope = search_filters(wording, turn.plan.get("timezone", "UTC"))
    kind = scope.get("record_type")
    if kind == "finding" or any(
        value.lower() not in KINDS for value in TYPE.findall(wording)
    ):
        return None
    quoted = literal(question)
    literal_request = re.search(
        r"\b(?:messages?|texts?|records?)\s+(?:(?:that|which)\s+)?"
        r"(?:contain(?:s|ing)?|mention(?:s|ing)?|say(?:s|ing)?"
        r"|with(?: the exact wording)?)"
        r"\s+QUOTE\b",
        QUOTED_TEXT.sub("QUOTE", question),
        re.I,
    )
    if len(set(TYPE.findall(wording))) > 1 or re.search(
        r"(?<!\S)(?:on|from|through):", DATE.sub("", wording), re.I
    ):
        return None
    try:
        for mode, value in DATE.findall(wording):
            if date.fromisoformat(value) == date.max and mode.lower() != "from":
                return None
    except ValueError:
        return None
    phrase = quoted if literal_request else ""
    if (
        not kind
        and (phrase or scope)
        and re.search(r"\b(?:messages?|texts?)\b", wording, re.I)
    ):
        kind = "text-message"
    if not kind and not scope.get("start") and not scope.get("end"):
        return None
    if kind == "text-message":
        scope = _scope(turn)
        if scope is None:
            return None
    selection = {
        "field": "count",
        "through": turn.started_at.isoformat(),
        "kinds": [kind] if kind else list(KINDS),
        "phrase": phrase if kind in ("text", "text-message") else "",
        **{k: scope[k].isoformat() for k in ("start", "end") if k in scope},
    }
    if scope.get("start") or scope.get("end"):
        selection["count_day"] = True
    return selection


def prepare_context(turn):
    from .search_budget import MAX_LOOKUPS, reserve_lookup, timed_query

    if turn.plan.get("lookup_attempts", 0) >= MAX_LOOKUPS - 1:
        return
    if engine.review_requested(turn.question) or turn.plan.get(
        "checked_message_wording"
    ):
        return
    if scope := context_selection(turn):
        # The population is supplemental to interpretation. A local timeout
        # leaves the ordinary answer and its bounded retrieval available.
        try:
            reserve_lookup(turn)
            with timed_query():
                _save_population(turn, scope | {"context_only": True})
        except AnalysisError as error:
            if error.code != "chat_budget_exhausted":
                raise
            turn.plan.pop("recorded_selection", None)


def offer_tool(turn):
    # Include the additional tool schema only when the investigator supplied
    # a plausible message field and a narrowing phrase, named party or date.
    words = QUOTED_TEXT.sub("", turn.question)
    if getattr(turn, "plan", {}).get("recorded_selection", {}).get("context_only"):
        return True
    return bool(
        not getattr(turn, "plan", {}).get("checked_message_wording")
        and RECORD_TOPIC.search(words)
        and (
            literal(turn.question)
            or DATE.search(words)
            or re.search(
                r"\b(?:from|to|between|contain\w*|mention\w*)\s+\w", words, re.I
            )
            or re.search(r"\b[A-Z][a-z]+\b", words.partition(" ")[2])
        )
    )


def _previous(turn):
    rows = ChatTurn.objects.filter(
        case_id=turn.case_id,
        chat_id=turn.chat_id,
        chat__case_id=turn.case_id,
        ordinal__lt=turn.ordinal,
        status="complete",
    )
    if explicit := TURN_NUMBER.search(turn.question):
        rows = rows.filter(ordinal=int(explicit[1]))
    return rows.order_by("-ordinal").first()


def _revision(case, lock=False):
    rows = CaseActivity.objects.filter(case=case)
    if lock:
        rows = rows.select_for_update()
    return rows.values("evidence", "people").get()


def available(turn, *, lock=False):
    from .search_budget import timed_query

    saved = turn.plan.get("recorded_selection")
    if not saved:
        return True
    if saved.get("snapshot") and not selections.snapshot_owner_exists(turn, saved):
        return False
    if _revision(turn.case, lock) == saved["revision"]:
        return True
    if saved["parties"] != _party_labels(turn.case, saved):
        return False
    with timed_query():
        if saved.get("snapshot"):
            return selections.current_membership(turn, saved)
        return (
            recorded_messages.membership(recorded_messages.matching(turn.case, saved))
            == saved["membership"]
        )


def _party_labels(case, selection):
    ids = [
        selection.get("sender_id"),
        selection.get("recipient_id"),
        *selection.get("pair", ()),
    ]
    return {
        str(person.pk): str(person)
        for person in Person.objects.filter(case=case, pk__in=ids)
    }


def _resolve(turn, value, key):
    if not value:
        return None, ""
    if value.casefold() in PRONOUN:
        previous = _previous(turn)
        if (
            previous
            and (selection := previous.plan.get("recorded_selection"))
            and available(previous)
        ):
            if person := selection.get(key):
                return person, ""
        if previous and previous.mode == "ordinary":
            from .services import inspect_turn

            if inspect_turn(previous) is not None:
                cited = {
                    ref["key"]
                    for claim in previous.answer["claims"]
                    for ref in claim["citations"]
                }
                ids = previous.sources.filter(
                    case=turn.case,
                    key__in=cited,
                    evidence_kind="text-message",
                    locator__surface="original",
                ).values("evidence_id")
                from core.evidence.queries import scoped_evidence

                parties = list(
                    scoped_evidence(turn.case, "text-message")
                    .filter(pk__in=ids, is_archived=False)
                    .order_by()
                    .values_list(key, flat=True)
                    .distinct()[:2]
                )
                if len(parties) == 1 and parties[0]:
                    person = Person.objects.get(case=turn.case, pk=parties[0])
                    if re.search(
                        r"(?<!\w)" + re.escape(str(person)) + r"(?!\w)",
                        QUOTED_TEXT.sub(" ", previous.question),
                        re.I,
                    ):
                        return str(person.pk), ""
        return (
            None,
            "Which recorded person do you mean? Give their full name or identifier.",
        )
    ids = _people(turn.case, value)
    if len(ids) == 1:
        return str(ids[0]), ""
    return (
        None,
        f"Which recorded person do you mean by “{value}”? "
        "Give their full name or identifier.",
    )


def _new_selection(turn, field, phrase, sender="", recipient=""):
    scope = _scope(turn)
    if scope is None:
        return None, "Which message type and valid event-date range should I use?"
    selection = {
        "field": field,
        "phrase": phrase,
        "through": turn.started_at.isoformat(),
        **{k: scope[k].isoformat() for k in ("start", "end") if k in scope},
    }
    if scope.get("start") or scope.get("end"):
        selection["count_day"] = True
    return _bind_people(
        turn, selection, {"sender_id": sender, "recipient_id": recipient}
    )


def count_selection(turn, filters):
    selection = {
        "field": "count",
        "phrase": "",
        "through": turn.started_at.isoformat(),
        "kinds": filters["kinds"],
    }
    if parties := filters.get("parties"):
        selection["pair"] = parties
    if "day" in filters:
        selection.update({k: v.isoformat() for k, v in day_bounds(filters).items()})
        selection["count_day"] = filters["day"]
    return selection


def _bind_people(turn, selection, names):
    for key, value in names.items():
        if key in selection:
            continue
        person, ask = _resolve(turn, value, key)
        if ask:
            turn.plan["recorded_clarification"] = {
                "selection": selection,
                "key": key,
                "names": names,
            }
            return None, ask
        if person:
            selection[key] = person
    return selection, ""


def from_model(turn, arguments):
    if not isinstance(arguments, dict) or arguments.get("field") not in FIELDS:
        raise AnalysisError("model_result_structure")
    phrase = arguments.get("phrase")
    quoted = literal(turn.question)
    if (
        not isinstance(phrase, str)
        or len(phrase) > 160
        or phrase
        and (not re.search(r"[^\W_]{3}", phrase) or phrase not in turn.question)
        or quoted
        and phrase != quoted
        or len(QUOTED_TEXT.findall(turn.question)) > 1
    ):
        raise AnalysisError("model_result_structure")
    selection = turn.plan.get("recorded_selection", {})
    if selection.get("context_only") and selection.get("kinds") != ["text-message"]:
        if (
            arguments["field"] not in ("count", "first", "last", "types", "weeks")
            or any(arguments.get(key) != "" for key in ("sender", "recipient"))
            or phrase
            and selection["kinds"] != ["text"]
        ):
            return (
                None,
                "Which dates, exact wording or evidence types should I compare?",
            )
        scope = selection | {"field": arguments["field"], "context_only": False}
        if phrase != scope["phrase"]:
            scope.pop("snapshot", None)
            scope["phrase"] = phrase
        return scope, ""
    if _scope(turn) is None:
        raise AnalysisError("model_result_structure")
    names = {}
    wording = QUOTED_TEXT.sub(" ", turn.question)
    for key in ("sender", "recipient"):
        value = arguments.get(key)
        if (
            not isinstance(value, str)
            or len(value) > 100
            or (value and value.casefold() not in wording.casefold())
        ):
            raise AnalysisError("model_result_structure")
        names[key] = value
    for role, clause in (
        ("sender", r"(?:messages? from|sent by)"),
        ("recipient", r"(?:addressed|sent|delivered) to"),
    ):
        match = re.search(r"\b" + clause + r"\s+(?P<party>[^,.;?!]+)", wording, re.I)
        if match and not (
            names[role]
            and re.match(re.escape(names[role]) + r"(?!\w)", match["party"], re.I)
        ):
            return None, f"Which recorded {role} should I use for the message lookup?"
    if (
        not phrase
        and not any(names.values())
        and not any(k in _scope(turn) for k in ("record_type", "start", "end"))
    ):
        return (
            None,
            "Which sender, recipient, dates or wording should I use?",
        )
    scope, ask = _new_selection(turn, arguments["field"], phrase, **names)
    if (
        scope
        and selection.get("context_only")
        and all(
            scope.get(key) == selection.get(key)
            for key in (
                "through",
                "start",
                "end",
                "count_day",
                "phrase",
                "sender_id",
                "recipient_id",
            )
        )
    ):
        return selection | {"field": arguments["field"], "context_only": False}, ""
    return scope, ask


def direct(turn):
    """Recognize inexpensive exact requests. Other wording uses the typed tool."""
    if engine.review_requested(turn.question):
        return None
    question = turn.question.strip()
    if follow := FOLLOW.fullmatch(question):
        previous = _previous(turn)
        if not previous:
            return None, "Which earlier answer in this conversation should I use?"
        saved = previous.plan.get("recorded_selection")
        if not saved or (saved.get("context_only") and not saved.get("phrase")):
            if follow["order"]:
                return {"previous": previous, "field": follow["order"].lower()}, ""
            return {"updates_from": previous}, ""
        previous_available = available(previous)
        if follow["order"] and not previous_available:
            return (
                None,
                "The earlier selection changed. Which records should I compare now?",
            )
        selection = {
            k: v
            for k, v in saved.items()
            if k
            not in (
                "membership",
                "revision",
                "parties",
                "retention",
                "coverage",
                "snapshot",
                "context_only",
            )
        }
        if follow["order"]:
            selection["field"] = follow["order"].lower()
            if saved.get("snapshot"):
                selection["snapshot"] = saved["snapshot"]
        elif saved.get("snapshot"):
            selection.pop("after", None)
            selection.update(
                updated_after=saved["through"],
                previous_snapshot=saved["snapshot"],
                through=turn.started_at.isoformat(),
                refreshed=not previous_available,
            )
        elif previous_available:
            selection.update(
                after=saved["through"], through=turn.started_at.isoformat()
            )
        else:
            # Archival or restoration changed the old population. Refresh the
            # scoped active set so newly eligible earlier records are included.
            selection.pop("after", None)
            selection.update(refreshed=True, through=turn.started_at.isoformat())
        return selection, ""
    if group := re.fullmatch(
        r"(?:group|break down|count|show)"
        r"(?: those| these| them| the results| the matches)?"
        r" by (week|type)(?: since| in| from)?(?: (?:answer|reply) #?\d+)?[?.!]?",
        question,
        re.I,
    ):
        previous = _previous(turn)
        if (
            previous
            and (saved := previous.plan.get("recorded_selection"))
            and saved.get("snapshot")
            and available(previous)
        ):
            return saved | {
                "field": "weeks" if group[1].lower() == "week" else "types",
                "context_only": False,
            }, ""
        return None, "Which earlier set of matching records should I group?"
    phrase = literal(question)
    if not phrase:
        if match := TEXT_PARTIES.fullmatch(question):
            return _new_selection(
                turn, "recipients", "", sender=match["party"] or match["source"]
            )
        if not engine.NEW_QUESTION.match(question) and len(question) <= 100:
            previous = _previous(turn)
            if previous and (pending := previous.plan.get("recorded_clarification")):
                return _bind_people(
                    turn,
                    pending["selection"] | {"through": turn.started_at.isoformat()},
                    pending["names"] | {pending["key"]: question.strip(".!? ")},
                )
        return None
    if _scope(turn) is None:
        return None
    shape = " ".join(
        DATE.sub("", TYPE.sub("", QUOTED_TEXT.sub("QUOTE", question))).split()
    )
    if match := RECIPIENT.fullmatch(shape):
        return _new_selection(turn, "recipients", phrase, sender=match["party"])
    if RECEIVERS.fullmatch(shape):
        return _new_selection(turn, "recipients", phrase)
    return None


def _evidence(turn, key, start, phrase, date_only):
    rows = corpus.rows(
        turn.case, "original", "text-message", False, through=turn.started_at
    ).filter(pk=key)
    # Preserve visible quotation/forward attribution immediately before the match.
    context_start = max(0, start - 90)
    size = start - context_start + len(phrase)
    item = corpus.clipped(rows, "body", context_start, size).first()
    if item is None:
        raise AnalysisError("source_changed")
    found = corpus.material(
        turn.case, "original", "text-message", item, False, context_start, size
    )
    if not found or found.text[start - context_start :] != phrase:
        raise AnalysisError("source_changed")
    locator = found.locator
    quote = engine.Window(locator, context_start, found.text)
    sender = str(item.sender)
    recipient = message_destination(item)
    to_field = "Reported recipient" if item.recipient_id else "Destination"
    fields = f"Reported sender: {sender}\n{to_field}: {recipient}"
    if item.occurred_at and not date_only:
        fields += f"\nOccurred: {item.occurred_at}"
    metadata, _, _ = corpus.original_metadata(item, "text-message")
    # Match only the generated field block; an investigator-supplied title
    # or source may contain an identical line of text.
    position = metadata.rfind(fields)
    if position < 0:
        raise AnalysisError("source_changed")
    details = engine.Window(locator, locator["metadata_start"] + position, fields)
    return item, (quote, details) if phrase else (details,)


def _save_population(turn, selection):
    revision = _revision(turn.case, lock=True)
    if snapshot := selection.get("snapshot"):
        original_turn = (
            turn
            if snapshot == str(turn.pk)
            else selections.snapshot_owner(turn, snapshot).first()
        )
        if original_turn is None or not available(original_turn):
            raise AnalysisError("source_changed")
    else:
        snapshot = selections.freeze(turn, selection)
    spec = selection | {"snapshot": snapshot}
    members = selections.totals(
        selections.snapshot_rows(turn, spec), only_timed=spec.get("count_day")
    )
    parties = _party_labels(turn.case, selection)
    expected = {
        selection.get("sender_id"),
        selection.get("recipient_id"),
        *selection.get("pair", ()),
    } - {None}
    if expected != parties.keys():
        raise AnalysisError("source_changed")
    spec.update(
        parties=parties,
        membership=members,
        revision=revision,
        coverage=selections.coverage(spec, members, 0),
    )
    turn.plan["recorded_selection"] = spec
    return spec


def execute(turn, selection, ask=""):
    from .search_budget import reserve_lookup, timed_query

    if ask:
        return engine.empty_answer(clarification=ask), []
    reserve_lookup(turn)
    if previous := selection.get("previous"):
        from .history import compare_cited_events

        return compare_cited_events(turn, previous, selection["field"])
    with timed_query():
        selection = _save_population(turn, selection)
        if selection.get("kinds", ["text-message"]) != ["text-message"] or selection[
            "field"
        ] in ("types", "weeks"):
            return selections.answer_for_fields(turn, selection)
        members = selection["membership"]
        parties = selection["parties"]
        rows = recorded_messages.from_snapshot(
            turn.case, selections.snapshot_rows(turn, selection)
        )
        matches = recorded_messages.representatives(rows, selection)
    field, phrase = selection["field"], selection["phrase"]
    answer = engine.empty_answer()
    windows = []
    for key, offset, date_only in matches[: recorded_messages.GROUP_LIMIT]:
        item, evidence = _evidence(turn, key, offset - 1, phrase, date_only)
        windows.extend(evidence)
        sender = str(item.sender)
        destination = message_destination(item)
        if not item.recipient_id:
            destination = (
                "a group chat in the source export (individual recipients unknown)"
            )
        claim = f"{sender} → {destination}"
        if phrase and re.match(r"\s*(?:fwd?:|forwarded\b)", evidence[0].text, re.I):
            claim += " (marked as forwarded)"
        if field in ("first", "last"):
            claim += f" at {item.occurred_at.isoformat()}"
        answer["claims"].append(
            {
                "text": claim,
                "citations": [
                    {
                        "key": w.locator["key"],
                        "start": w.start,
                        "end": w.start + len(w.text),
                        "quote": w.text,
                    }
                    for w in evidence
                ],
            }
        )
    selection["coverage"]["inspected"] = len({w.locator["record_key"] for w in windows})
    confirmed = members["count"]
    if selection.get("count_day"):
        confirmed -= members["undated"] + members["date_only"]
    quantity = "message" if confirmed == 1 else "messages"
    summary = f"{confirmed} active text {quantity} matched the recorded filters"
    if phrase:
        summary += f" and contain the exact wording “{phrase}”"
    summary += "."
    if pair := selection.get("pair"):
        first, second = (parties[key] for key in pair)
        summary += (
            f" Counted exchanges in either direction between {first} and {second}."
        )
    for role in ("sender", "recipient"):
        if person_id := selection.get(role + "_id"):
            summary += f" Recorded {role}: {parties[person_id]}."
    if day := selection.get("count_day"):
        if isinstance(day, str):
            summary += f" The selected local event date is {day}."
        if field == "count" and (members["undated"] or members["date_only"]):
            summary += (
                " Additional records excluded from the dated total: "
                f"{members['undated']} without an event time and "
                f"{members['date_only']} with only an export calendar date."
            )
    elif selection.get("start") or selection.get("end"):
        summary += " The date filter includes messages with a precise occurrence time."
    if selection.get("refreshed"):
        summary += " Earlier membership changed. Showing new or restored matches."
    elif selection.get("after") or selection.get("updated_after"):
        summary += " This selection covers new or restored matches since that answer."
    if field in ("recipients", "senders"):
        summary += f" Showing one recorded exchange per distinct {field[:-1]}."
    elif field in ("first", "last"):
        summary += f" Showing the {field} precisely timed occurrence, including ties."
        if members["date_only"]:
            summary += (
                f" {members['date_only']} matching messages give only a calendar date."
            )
        if members["undated"]:
            summary += (
                f" {members['undated']} matching messages have no recorded event time."
            )
    if len(matches) > recorded_messages.GROUP_LIMIT:
        summary += f" Only the first {recorded_messages.GROUP_LIMIT} results are shown."
    summary += (
        " Sender and destination fields identify the recorded exchange. "
        "Quoted or forwarded wording does not establish authorship or who read it."
        " Spoken accounts and paraphrases fall outside this field query."
    )
    answer["summary"] = summary
    return answer, windows


def deletion_dependencies(case, message_ids):
    """Resolve only the message IDs in a reviewed permanent-deletion inventory."""
    keys = {str(pk) for pk in message_ids}
    turns = ChatTurn.objects.filter(
        case=case, chat__case=case, plan__has_key="recorded_selection"
    ).exclude(plan__recorded_selection__has_key="snapshot")
    affected = []
    for pk, selection in turns.values_list("pk", "plan__recorded_selection").iterator(
        chunk_size=200
    ):
        scope = selection["retention"]
        if "included" in scope:
            matched = not keys.isdisjoint(scope["included"])
        else:
            candidates = keys.difference(scope["excluded"])
            matched = (
                bool(candidates)
                and recorded_messages.matching(case, selection, archived=None)
                .filter(pk__in=candidates)
                .exists()
            )
        if matched:
            affected.append(pk)
    return affected
