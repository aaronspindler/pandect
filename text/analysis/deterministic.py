"Bounded text observations with exact offsets and explicit contextual limits."

import re
import unicodedata
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from itertools import combinations
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError

from core.analysis.sources import output
from text.analysis.shared import FUNCTION_WORDS, STOPWORDS, WORD, bounded_sources

VERSION = "text-rules-v1"
ENTITY_VERSION = "entity-rules-v2"
RELATIVE_DATES = ("today", "tomorrow", "yesterday")
ABBREVIATIONS = set(
    "lol lmao brb btw idk imo imho rn tbh ttyl asap fyi omg thx pls u ur bc".split()
)
# Deliberately conservative: unsupported TLDs remain outside this baseline,
# rather than calling ordinary file extensions or dotted prose a domain.
BARE_DOMAIN_TLDS = set(
    "com org net edu gov mil int io ai co uk ca au de fr nl ch se es it jp "
    "info biz app dev me tv us eu nz za in br mx sg hk ie be dk no fi pl "
    "test example invalid onion".split()
)
BARE_DOMAIN = re.compile(
    r"(?<![\w@/.-])"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63}(?![\w-]|\.[\w-])"
)


SPELLING_VARIANTS = (
    "color",
    "colour",
    "center",
    "centre",
    "organize",
    "organise",
    "realize",
    "realise",
    "favorite",
    "favourite",
    "okay",
    "ok",
)


def _item(source, label, text, start, end, **values):
    return {
        "label": label,
        "text": text,
        "source_ids": [str(source.id)],
        "anchors": [source.anchor(start=start, end=end)],
        "values": values,
    }


def extract_entities(sources, parameters):
    "Extract explicit strings only; normalization never asserts an identity."
    sources = bounded_sources(sources)
    patterns = (
        (
            "Email",
            re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,63}(?![\w-])"),
        ),
        ("URL", re.compile(r"https?://[^\s<>\"']+", re.I)),
        ("Domain candidate", BARE_DOMAIN),
        ("Phone candidate", re.compile(r"(?<!\w)\+?\d[\d () .-]{5,}\d(?!\w)")),
        (
            "Date candidate",
            re.compile(
                rf"\b(?:{'|'.join(RELATIVE_DATES)}|"
                r"\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|"
                r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
                r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
                r"Dec(?:ember)?)\s+\d{1,2}(?:,?\s+\d{4})?)\b",
                re.I,
            ),
        ),
        (
            "Amount candidate",
            re.compile(
                r"(?<!\w)(?:(?:USD|CAD|EUR|GBP|AUD|JPY)\s*|[$€£¥])\d[\d,.]*(?:\s*(?:USD|CAD|EUR|GBP|AUD|JPY))?\b",
                re.I,
            ),
        ),
        (
            "Identifier candidate",
            re.compile(
                r"\b(?:[A-Za-z][A-Za-z0-9_-]{1,20}[-_]\d[A-Za-z0-9_-]{1,40}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\b"
            ),
        ),
        (
            "Coordinate candidate",
            re.compile(
                r"(?<![\w.])([+-]?\d{1,2}(?:\.\d+)?)\s*,\s*([+-]?\d{1,3}(?:\.\d+)?)(?![\w.])"
            ),
        ),
    )
    items = []
    for source in sources:
        for label, pattern in patterns:
            for match in pattern.finditer(source.text):
                raw = match.group()
                start, end = match.span()
                values = {
                    "raw": raw,
                    "normalization": "unresolved",
                    "method": ENTITY_VERSION,
                }
                if label == "URL":
                    raw = raw.rstrip(".,;!?)")
                    end = start + len(raw)
                    try:
                        parsed = urlsplit(raw)
                        if not parsed.hostname or parsed.username or parsed.password:
                            continue
                        values |= {
                            "raw": raw,
                            "host_candidate": parsed.hostname.lower(),
                            "normalization": "URL host lowercased; path preserved",
                        }
                    except ValueError:
                        continue
                elif label == "Domain candidate":
                    candidate = raw.lower()
                    if (
                        len(candidate) > 253
                        or candidate.rsplit(".", 1)[-1] not in BARE_DOMAIN_TLDS
                    ):
                        continue
                    values |= {
                        "candidate": candidate,
                        "normalization": (
                            "ASCII domain lowercased; no DNS lookup performed"
                        ),
                        "interpretation": (
                            "Explicit bare-domain string candidate; no ownership, "
                            "availability, or safety claim"
                        ),
                    }
                elif label == "Email":
                    local, domain = raw.rsplit("@", 1)
                    values |= {
                        "candidate": f"{local}@{domain.lower()}",
                        "normalization": "domain lowercased; local part preserved",
                    }
                elif label == "Phone candidate":
                    digits = re.sub(r"\D", "", raw)
                    if not 7 <= len(digits) <= 15 or re.fullmatch(
                        r"\d{4}-\d{2}-\d{2}", raw
                    ):
                        continue
                    values["digits_candidate"] = digits
                elif label == "Date candidate":
                    if raw.casefold() in RELATIVE_DATES:
                        values["relative"] = True
                    values["interpretation"] = (
                        "Relative date with no established reference date."
                        if values.get("relative")
                        else "Source date string; order, timezone, and relative "
                        "context are not inferred."
                    )
                elif label == "Amount candidate":
                    values["interpretation"] = (
                        "Source amount string; a currency symbol alone may be "
                        "ambiguous."
                    )
                elif label == "Coordinate candidate":
                    latitude, longitude = map(float, match.groups())
                    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                        continue
                    values |= {
                        "latitude_candidate": latitude,
                        "longitude_candidate": longitude,
                        "interpretation": (
                            "Assumes latitude, longitude order; requires review."
                        ),
                    }
                items.append(_item(source, label, raw, start, end, **values))
                if len(items) >= 2000:
                    return output(
                        "Extracted candidate strings; occurrence limit reached.",
                        items=items,
                        warnings=[
                            (
                                "Only the first 2,000 occurrences are included. Select "
                                "less text to inspect remaining occurrences."
                            )
                        ],
                        outcome="partial",
                    )
    return output(
        f"Found {len(items)} explicit-string candidates.",
        items=items,
        metrics={
            "sources": len(sources),
            "occurrences": len(items),
            "method": ENTITY_VERSION,
        },
        warnings=[
            (
                "Names, organizations, and free-form addresses are not "
                "guessed by this deterministic extractor. Mentions do "
                "not establish ownership or association. Bare domains use a "
                "conservative documented ASCII/TLD subset; IDNs and other "
                "suffixes need separately evaluated support."
            )
        ],
        outcome="complete" if items else "empty",
    )


def _exclusion_spans(source, parameters):
    spans = parameters.get("exclusions", {}).get(str(source.id), [])
    if not isinstance(spans, list) or len(spans) > 200:
        raise ValidationError("Supply at most 200 exclusion spans per source.")
    validated = []
    for span in spans:
        if not isinstance(span, (list, tuple)) or len(span) != 2:
            raise ValidationError("Exclusions require start and end character offsets.")
        start, end = span
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int)
            or not isinstance(end, int)
            or not 0 <= start < end <= len(source.text)
        ):
            raise ValidationError("An exclusion lies outside its exact source text.")
        validated.append((start, end, "Investigator-selected exclusion"))
    phrases = parameters.get("exclude_phrases", "")
    if not isinstance(phrases, str) or len(phrases) > 8000:
        raise ValidationError("Excluded wording must contain at most 8,000 characters.")
    for phrase in dict.fromkeys(line for line in phrases.splitlines() if line):
        position = 0
        while (start := source.text.find(phrase, position)) >= 0:
            validated.append(
                (
                    start,
                    start + len(phrase),
                    "Investigator-selected copied or quoted wording",
                )
            )
            if len(validated) > 200:
                raise ValidationError(
                    "Selected wording produces more than 200 exclusion spans; "
                    "select narrower samples."
                )
            position = start + len(phrase)
    if parameters.get("exclude_quotes", True):
        for match in re.finditer(
            r'(?m)^\s*>[^\n]*(?:\n|$)|"[^"\n]{2,}"|“[^”\n]{2,}”', source.text
        ):
            validated.append(
                (*match.span(), "Quotation candidate excluded under selected rule")
            )
    if len(validated) > 200:
        raise ValidationError("A source has more than 200 exclusion spans.")
    return sorted(validated)


def _profile_data(sources, parameters):
    sources = bounded_sources(sources)
    supported = [
        s
        for s in sources
        if s.kind in {"text", "text-message"}
        and not s.extracted_text
        and not s.attachment
    ]
    contexts = parameters.get("contexts", {})
    if not isinstance(contexts, dict):
        raise ValidationError("Sample contexts must be keyed by source ID.")
    if set(parameters.get("exclusions", {})) - {str(s.id) for s in sources}:
        raise ValidationError("An exclusion refers to an unselected source.")
    usable = []
    exclusions = []
    for source in supported:
        context = contexts.get(str(source.id), {})
        if not isinstance(context, dict):
            raise ValidationError("Each sample context must be an object.")
        if context.get("origin", "original") != "original":
            exclusions.append(
                {
                    "source_id": str(source.id),
                    "start": 0,
                    "end": len(source.text),
                    "reason": (
                        "Translated, edited, dictated, OCR, or transcription "
                        "context excluded"
                    ),
                }
            )
            continue
        text = list(source.text)
        for start, end, reason in _exclusion_spans(source, parameters):
            text[start:end] = " " * (end - start)
            exclusions.append(
                {
                    "source_id": str(source.id),
                    "start": start,
                    "end": end,
                    "reason": reason,
                }
            )
        retained_text = "".join(text)
        if WORD.search(retained_text):
            usable.append((source, retained_text))
    texts = [text for _, text in usable]
    words = [m.group().casefold() for text in texts for m in WORD.finditer(text)]
    counts = Counter(words)
    all_text = "\n".join(texts)
    letters = [c for c in all_text if c.isalpha()]
    sentences = [
        s for text in texts for s in re.split(r"[.!?]+(?:\s|$)", text) if WORD.search(s)
    ]
    symbols = Counter(c for c in all_text if unicodedata.category(c) == "So")
    phrase_counts, _ = _phrase_occurrences(usable, exclusions)
    language_labels = Counter(
        str(
            contexts.get(str(s.id), {}).get("language")
            or getattr(s.item, "language", "")
            or "unknown"
        )
        for s, _ in usable
    )
    unique_ratio = len(counts) / len(words) if words else 0
    # This is a disclosed product adequacy guard, never an attribution-accuracy claim.
    adequate = len(words) >= 30 and len(counts) >= 12 and unique_ratio >= 0.1
    metrics = {
        "sources_selected": len(sources),
        "sources_usable": len(usable),
        "words": len(words),
        "unique_words": len(counts),
        "characters": len(all_text),
        "excluded_spans": len(exclusions),
        "uppercase_letter_ratio": round(
            sum(c.isupper() for c in letters) / len(letters), 4
        )
        if letters
        else None,
        "mean_message_words": round(len(words) / len(texts), 2) if texts else None,
        "mean_sentence_words": round(
            sum(len(WORD.findall(s)) for s in sentences) / len(sentences), 2
        )
        if sentences
        else None,
        "punctuation_per_100_words": round(
            sum(unicodedata.category(c).startswith("P") for c in all_text)
            * 100
            / len(words),
            2,
        )
        if words
        else None,
        "vocabulary_ratio": round(unique_ratio, 4),
        "method": VERSION,
    }
    features = {
        "Punctuation": dict(
            Counter(
                c for c in all_text if unicodedata.category(c).startswith("P")
            ).most_common(20)
        ),
        "Capitalization": {
            "uppercase_letters": sum(c.isupper() for c in letters),
            "letters": len(letters),
        },
        "Spelling variants": {w: counts[w] for w in SPELLING_VARIANTS if counts[w]},
        "Vocabulary": dict(counts.most_common(20)),
        "Function words": {w: counts[w] for w in sorted(FUNCTION_WORDS) if counts[w]},
        "Abbreviations": {w: counts[w] for w in sorted(ABBREVIATIONS) if counts[w]},
        "Emoji and other Unicode symbols": dict(symbols.most_common(20)),
        "Repeated three-word phrases": {
            phrase: count
            for phrase, count in phrase_counts.most_common(20)
            if count > 1
        },
        "Supplied language context": dict(language_labels),
    }
    return usable, exclusions, metrics, features, adequate


def style_profile(sources, parameters):
    usable, exclusions, metrics, features, adequate = _profile_data(sources, parameters)
    if not any(WORD.search(text) for _, text in usable):
        return output(
            "Insufficient usable writing evidence.",
            metrics=metrics,
            warnings=[
                "No usable original writing remains after the selected exclusions.",
                (
                    "Derived, translated, and transcribed formatting "
                    "is not a writer profile."
                ),
            ],
            outcome="insufficient",
        )
    items = []
    for label, values in features.items():
        examples = []
        for source, text in usable:
            if not text.strip():
                continue
            terms = list(values)
            if label == "Capitalization":
                matches = [m for m in re.finditer(r"\S", text) if m.group().isupper()][
                    :3
                ]
            elif label == "Supplied language context":
                matches = [next(WORD.finditer(text), None)]
            elif label == "Punctuation":
                matches = [
                    m for m in re.finditer(r"[^\w\s]", text) if m.group() in values
                ][:3]
            else:
                matches = [
                    m
                    for term in terms
                    for m in re.finditer(
                        re.escape(term)
                        if label == "Emoji and other Unicode symbols"
                        else r"(?<!\w)" + re.escape(term) + r"(?!\w)",
                        text,
                        re.I,
                    )
                ][:3]
            for match in matches:
                if match and any(
                    start <= match.start() <= match.end() <= end
                    for start, end in _usable_ranges(source, exclusions)
                ):
                    examples.append(source.anchor(start=match.start(), end=match.end()))
            if len(examples) >= 6:
                break
        items.append(
            {
                "label": label,
                "text": "Counts describe the selected usable text.",
                "source_ids": [str(s.id) for s, _ in usable],
                "anchors": examples[:6],
                "values": values,
            }
        )
    for source, text in usable:
        match = re.match(
            r"\s*(?P<greeting>hi|hey|hello|dear|good morning|good evening)\b",
            text,
            re.I,
        )
        if match:
            items.append(
                _item(
                    source,
                    "Greeting",
                    source.text[slice(*match.span("greeting"))],
                    *match.span("greeting"),
                )
            )
        match = re.search(
            r"\b(?P<signoff>regards|sincerely|cheers|bye|goodnight|thanks)[.!\s]*$",
            text,
            re.I,
        )
        if match:
            items.append(
                _item(
                    source,
                    "Sign-off",
                    source.text[slice(*match.span("signoff"))],
                    *match.span("signoff"),
                )
            )
    for span in exclusions:
        items.append(
            {
                "label": "Excluded sample span",
                "text": span["reason"],
                "source_ids": [span["source_id"]],
                "anchors": [
                    dict(
                        source_id=span["source_id"],
                        start=span["start"],
                        end=span["end"],
                    )
                ],
                "values": {},
            }
        )
    warnings = [
        (
            "These observations describe selected wording and "
            "formatting; they do not establish authorship, "
            "identity, personality, intent, mental state, or "
            "truthfulness."
        ),
        (
            "Attribution may describe a shared account. Topic, "
            "channel, recipient, editing, autocorrect, and "
            "dictation can change observed features."
        ),
        (
            "Language labels are supplied context. Automatic "
            "language detection and code-switching boundaries are "
            "unsupported; language mixtures remain disclosed "
            "context."
        ),
        (
            "Unicode-symbol counts include some non-emoji symbols "
            "and count code points rather than complete emoji "
            "sequences. Spelling variants are observed forms, not "
            "spelling-error diagnoses."
        ),
        (
            "Adequacy guard: at least 30 usable words, 12 distinct "
            "words, and 0.1 vocabulary ratio. This is a "
            "conservative display rule, not a validated authorship "
            "threshold."
        ),
    ]
    if not adequate:
        warnings.append(
            "Insufficient evidence for a useful descriptive "
            "profile; counts remain inspectable without a "
            "comparison conclusion."
        )
    if metrics["sources_usable"] < metrics["sources_selected"]:
        warnings.append(
            "Only original Text/Text message samples were included;"
            " derived/unsupported or explicitly non-original "
            "samples were excluded."
        )
    return output(
        "Descriptive writing profile."
        if adequate
        else "Insufficient usable writing evidence.",
        items=items,
        metrics=metrics,
        warnings=warnings,
        outcome="complete" if adequate else "insufficient",
    )


def _usable_ranges(source, exclusions):
    """Complement exact excluded spans; examples must never cross an exclusion."""
    spans = sorted(
        (entry["start"], entry["end"])
        for entry in exclusions
        if entry["source_id"] == str(source.id)
    )
    ranges = []
    cursor = 0
    for start, end in spans:
        if cursor < start:
            ranges.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < len(source.text):
        ranges.append((cursor, len(source.text)))
    return ranges


def _phrase_occurrences(usable, exclusions):
    counts = Counter()
    examples = defaultdict(list)
    for source, text in usable:
        for start, end in _usable_ranges(source, exclusions):
            words = list(WORD.finditer(text, start, end))
            for index in range(len(words) - 2):
                selected = words[index : index + 3]
                phrase = " ".join(word.group().casefold() for word in selected)
                counts[phrase] += 1
                if len(examples[phrase]) < 4:
                    examples[phrase].append(
                        source.anchor(start=selected[0].start(), end=selected[-1].end())
                    )
    return counts, examples


def _sample_measurement(text, feature):
    words = WORD.findall(text)
    letters = [character for character in text if character.isalpha()]
    if feature == "uppercase_letter_ratio":
        return sum(c.isupper() for c in letters) / len(letters) if letters else None
    if feature == "punctuation_per_100_words":
        return (
            sum(unicodedata.category(c).startswith("P") for c in text)
            * 100
            / len(words)
            if words
            else None
        )
    if feature == "mean_message_words":
        return len(words)
    if feature == "mean_sentence_words":
        sentences = [
            row for row in re.split(r"[.!?]+(?:\s|$)", text) if WORD.search(row)
        ]
        return len(words) / len(sentences) if sentences else None
    if feature == "vocabulary_ratio":
        return len({word.casefold() for word in words}) / len(words) if words else None
    raise ValueError("Unsupported descriptive writing measurement")


def _style_examples(profile, feature, group):
    usable, exclusions = profile[:2]
    examples = []
    for source, text in usable:
        measurement = _sample_measurement(text, feature)
        if measurement is None:
            continue
        candidates = []
        for start, end in _usable_ranges(source, exclusions):
            if feature == "uppercase_letter_ratio":
                candidates.extend(
                    (index, index + 1)
                    for index in range(start, end)
                    if text[index].isupper()
                )
            elif feature == "punctuation_per_100_words":
                candidates.extend(
                    (index, index + 1)
                    for index in range(start, end)
                    if unicodedata.category(text[index]).startswith("P")
                )
            elif feature == "mean_sentence_words":
                candidates.extend(
                    (start + match.start(), start + match.end())
                    for match in re.finditer(r"[^.!?]+[.!?]?", text[start:end])
                    if WORD.search(match.group())
                )
            elif feature == "mean_message_words":
                match = WORD.search(text, start, end)
                if match:
                    candidates.append((match.start(), min(end, match.start() + 240)))
            if candidates:
                break
        if not candidates:
            for start, end in _usable_ranges(source, exclusions):
                match = WORD.search(text, start, end)
                if match:
                    candidates.append(match.span())
                    break
        if not candidates:
            continue
        start, end = candidates[0]
        examples.append(
            {
                **source.anchor(start=start, end=end),
                "group": group,
                "excerpt": source.text[start:end],
                "source_measurement": round(measurement, 4),
                "usable_source_words": len(WORD.findall(text)),
                "scope": (
                    "Measurement uses this source's usable text; the linked"
                    " passage is a feature example, not an authorship claim"
                ),
            }
        )
    return examples


def _feature_comparison_examples(profiles, feature, a, b, comparable):
    examples = [
        example
        for index, profile in enumerate(profiles)
        for example in _style_examples(profile, feature, "A" if index == 0 else "B")
    ]
    support, contrary, mixed = [], [], []
    for example in examples:
        value = example["source_measurement"]
        if not comparable or a is None or b is None or a == b:
            mixed.append(example)
            continue
        increasing = b > a
        if example["group"] == "A":
            consistent = value <= a if increasing else value >= a
            counter = value > b if increasing else value < b
        else:
            consistent = value >= b if increasing else value <= b
            counter = value < a if increasing else value > a
        (contrary if counter else support if consistent else mixed).append(example)

    def balanced(examples, count):
        return [
            example
            for group in ("A", "B")
            for example in [row for row in examples if row["group"] == group][:count]
        ]

    support = balanced(support, 2)
    contrary = balanced(contrary, 2)
    mixed = balanced(mixed, 1)
    chosen = support + contrary + mixed
    return chosen, {
        "supporting_examples": support,
        "contrary_examples": contrary,
        "mixed_or_shared_examples": mixed,
        "example_basis": (
            "Per-source usable-text measurements compared with "
            "disclosed group aggregates; omitted contrary examples "
            "are not inferred to exist"
        ),
    }


def _profile_time_coverage(profile, parameters):
    usable = profile[0]
    times = []
    for source, _ in usable:
        timestamp = (
            getattr(source.item, "occurred_at", None)
            if source.kind == "text-message"
            else None
        )
        if (
            timestamp is not None
            and timestamp.tzinfo is not None
            and timestamp.utcoffset() is not None
        ):
            times.append((source, timestamp))
    contexts = parameters.get("contexts", {})
    values = {
        "sources_with_reported_event_time": len(times),
        "sources_missing_reported_event_time": len(usable) - len(times),
        "first_reported_event_at": min((time for _, time in times)).isoformat()
        if times
        else None,
        "last_reported_event_at": max((time for _, time in times)).isoformat()
        if times
        else None,
        "time_basis": (
            "Reported message event times only; upload and "
            "collection times are not writing times"
        ),
        "channels": dict(
            Counter(
                str(contexts.get(str(source.id), {}).get("channel") or source.kind)
                for source, _ in usable
            )
        ),
    }
    return values, times


def style_changes(sources, parameters):
    sources = bounded_sources(sources)
    group_a = parameters.get("group_a", [])
    group_b = parameters.get("group_b", [])
    if (
        not isinstance(group_a, list)
        or not isinstance(group_b, list)
        or not group_a
        or not group_b
    ):
        raise ValidationError("Select two nonempty source groups for comparison.")
    groups = [set(map(str, group_a)), set(map(str, group_b))]
    source_ids = {str(source.id) for source in sources}
    if groups[0] & groups[1] or not (groups[0] | groups[1]) <= source_ids:
        raise ValidationError("Comparison groups must be disjoint selected-source IDs.")
    if set(parameters.get("exclusions", {})) - source_ids:
        raise ValidationError("An exclusion refers to an unselected source.")
    profiles = [
        _profile_data(
            [source for source in sources if str(source.id) in group],
            parameters
            | {
                "exclusions": {
                    key: spans
                    for key, spans in parameters.get("exclusions", {}).items()
                    if key in group
                }
            },
        )
        for group in groups
    ]
    comparable = all(profile[4] for profile in profiles)
    items = []
    differences = []
    for key in (
        "uppercase_letter_ratio",
        "mean_message_words",
        "mean_sentence_words",
        "punctuation_per_100_words",
        "vocabulary_ratio",
    ):
        a, b = (profile[2][key] for profile in profiles)
        examples, explanation = _feature_comparison_examples(
            profiles, key, a, b, comparable
        )
        difference = (
            round(b - a, 4) if a is not None and b is not None and comparable else None
        )
        if difference:
            differences.append(key)
        items.append(
            {
                "label": key.replace("_", " ").capitalize(),
                "text": (
                    "Descriptive group difference with supporting, "
                    "contrary, and shared sample examples where observed; "
                    "no new-author inference."
                ),
                "source_ids": sorted(groups[0] | groups[1]),
                "anchors": [
                    {key: example[key] for key in ("source_id", "start", "end")}
                    for example in examples
                ],
                "values": {
                    "group_a": a,
                    "group_b": b,
                    "difference_b_minus_a": difference,
                    **explanation,
                },
            }
        )
    coverage = [_profile_time_coverage(profile, parameters) for profile in profiles]
    for index, profile in enumerate(profiles):
        examples = _style_examples(
            profile, "mean_message_words", "A" if index == 0 else "B"
        )[:4]
        items.append(
            {
                "label": f"Group {'A' if index == 0 else 'B'} coverage",
                "text": (
                    "Exact sample selection, usable text, exclusions, and "
                    "reported-event coverage; unknown times stay unknown."
                ),
                "source_ids": sorted(groups[index]),
                "anchors": [
                    {key: example[key] for key in ("source_id", "start", "end")}
                    for example in examples
                ],
                "values": profile[2]
                | coverage[index][0]
                | {"excluded_spans": profile[1]},
            }
        )
    phrase_groups = [
        _phrase_occurrences(profile[0], profile[1]) for profile in profiles
    ]
    phrases = sorted(
        set(phrase_groups[0][0]) | set(phrase_groups[1][0]),
        key=lambda phrase: (
            -(phrase_groups[0][0][phrase] + phrase_groups[1][0][phrase]),
            phrase,
        ),
    )
    for phrase in [
        phrase
        for phrase in phrases
        if sum(row[0][phrase] for row in phrase_groups) >= 2
    ][:20]:
        counts = [row[0][phrase] for row in phrase_groups]
        anchors = [anchor for row in phrase_groups for anchor in row[1].get(phrase, [])]
        items.append(
            {
                "label": "Recurring wording comparison",
                "text": phrase,
                "source_ids": sorted(groups[0] | groups[1]),
                "anchors": anchors,
                "values": {
                    "group_a_occurrences": counts[0],
                    "group_b_occurrences": counts[1],
                    "basis": (
                        "Three-word sequence in contiguous usable source "
                        "ranges; case-folded comparison with original spans"
                    ),
                    "interpretation": (
                        "Shared wording may reflect a stable habit, topic, "
                        "quotation or source dependence; it does not establish "
                        "authorship"
                    ),
                },
            }
        )
    interval = None
    if (
        comparable
        and differences
        and all(
            len(row[1]) == len(profile[0]) and row[1]
            for row, profile in zip(coverage, profiles, strict=True)
        )
    ):
        a_times, b_times = (row[1] for row in coverage)
        if max(time for _, time in a_times) < min(time for _, time in b_times):
            before, after = (
                max(a_times, key=lambda row: row[1]),
                min(b_times, key=lambda row: row[1]),
            )
        elif max(time for _, time in b_times) < min(time for _, time in a_times):
            before, after = (
                max(b_times, key=lambda row: row[1]),
                min(a_times, key=lambda row: row[1]),
            )
        else:
            before = after = None
        if before is not None:
            interval = {
                "start": before[1].isoformat(),
                "end": after[1].isoformat(),
                "differing_features": differences,
                "basis": (
                    "Gap between selected nonoverlapping reported-event "
                    "periods; does not locate an actual change point"
                ),
            }
            items.append(
                {
                    "label": "Possible observed-style change interval",
                    "text": (
                        "The selected periods have descriptive differences. The"
                        " gap between them is a candidate comparison interval, "
                        "not an observed change or evidence of a new writer."
                    ),
                    "source_ids": [str(before[0].id), str(after[0].id)],
                    "anchors": [],
                    "values": interval,
                }
            )
    return output(
        "Compared descriptive writing features."
        if comparable
        else "Insufficient evidence in one or both groups; no change conclusion.",
        items=items,
        metrics={
            "method": VERSION,
            "group_a_sources": len(groups[0]),
            "group_b_sources": len(groups[1]),
            "adequate_groups": sum(profile[4] for profile in profiles),
            "possible_change_interval": interval,
        },
        warnings=[
            (
                "Period, channel, recipient, topic, language, "
                "collaboration, editing, autocorrect, and dictation "
                "confound style changes. Differences do not prove a "
                "change of author."
            ),
            (
                "Unequal samples and short/repetitive texts limit "
                "comparisons. Supporting and contrary examples are "
                "observed source-level measurements, not calibrated "
                "attribution evidence; no identity ranking is produced."
            ),
            (
                "A possible interval is shown only for adequate, fully "
                "timed, nonoverlapping selected groups with observed "
                "feature differences. Unknown/overlapping times do not "
                "produce a change interval; missing periods remain "
                "unobserved."
            ),
        ],
        outcome="complete" if comparable else "insufficient",
    )


def copied_text(sources, parameters):
    sources = bounded_sources(sources)
    if len(sources) > 50:
        raise ValidationError(
            "Passage comparison supports at most 50 selected sources."
        )
    minimum = parameters.get("minimum_words", 8)
    if (
        isinstance(minimum, bool)
        or not isinstance(minimum, int)
        or not 5 <= minimum <= 30
    ):
        raise ValidationError("Minimum matching passage length must be 5–30 words.")
    if sum(len(s.text) for s in sources) > 100_000:
        raise ValidationError(
            "Passage comparison supports at most 100,000 selected characters."
        )
    prepared = []
    for source in sources:
        words = list(WORD.finditer(source.text))
        if len(words) > 5000:
            raise ValidationError("Select sources with no more than 5,000 words each.")
        prepared.append((source, words, [match.group().casefold() for match in words]))
    items = []
    for (left, left_words, a), (right, right_words, b) in combinations(prepared, 2):
        matcher = SequenceMatcher(None, a, b, autojunk=True)
        blocks = [
            block for block in matcher.get_matching_blocks() if block.size >= minimum
        ]
        if not blocks:
            # A bounded lightly edited passage can have shorter exact runs.
            if len(a) < minimum or len(b) < minimum or max(len(a), len(b)) > 1000:
                continue
            ratio = matcher.ratio()
            if ratio < 0.85:
                continue
            spans = [(0, len(left.text)), (0, len(right.text))]
            basis = "Whole-text token alignment; at least 85% similarity"
        else:
            block = max(blocks, key=lambda x: (x.size, -x.a, -x.b))
            spans = [
                (
                    left_words[block.a].start(),
                    left_words[block.a + block.size - 1].end(),
                ),
                (
                    right_words[block.b].start(),
                    right_words[block.b + block.size - 1].end(),
                ),
            ]
            ratio = matcher.ratio()
            basis = (
                f"Case-folded exact run of {block.size} words; "
                "whitespace and punctuation ignored for comparison"
            )
        items.append(
            {
                "label": "Candidate reused wording",
                "text": left.text[slice(*spans[0])],
                "source_ids": [str(left.id), str(right.id)],
                "anchors": [
                    left.anchor(start=spans[0][0], end=spans[0][1]),
                    right.anchor(start=spans[1][0], end=spans[1][1]),
                ],
                "values": {
                    "basis": basis,
                    "token_alignment_ratio": round(ratio, 4),
                    "minimum_words": minimum,
                    "right_passage": right.text[slice(*spans[1])],
                    "exclusion_candidate": {
                        str(left.id): [list(spans[0])],
                        str(right.id): [list(spans[1])],
                    },
                },
            }
        )
        if len(items) >= 200:
            return output(
                "Found candidate reused wording; comparison limit reached.",
                items=items,
                warnings=["Only the first 200 matching source pairs are retained."],
                outcome="partial",
            )
    return output(
        f"Found {len(items)} candidate reused passages.",
        items=items,
        metrics={"sources": len(sources), "minimum_words": minimum, "method": VERSION},
        warnings=[
            (
                "Common wording, templates, quotations, and repeated "
                "exports can explain matches. Shared wording does not "
                "establish authorship, independent corroboration, or "
                "truth. Review spans before adding them to style "
                "exclusions."
            )
        ],
        outcome="complete" if items else "empty",
    )


def keywords(sources, parameters):
    sources = bounded_sources(sources)
    by_word = defaultdict(list)
    for source in sources:
        for match in WORD.finditer(source.text):
            word = match.group().casefold()
            if len(word) >= 3 and word not in STOPWORDS:
                by_word[word].append((source, match.start(), match.end()))
    terms = sorted(by_word, key=lambda x: (-len(by_word[x]), x))[:20]
    items = []
    for term in terms:
        matches = by_word[term]
        items.append(
            {
                "label": term,
                "text": "Frequent observed word; a candidate content label.",
                "source_ids": list(dict.fromkeys(str(s.id) for s, _, _ in matches)),
                "anchors": [
                    s.anchor(start=start, end=end) for s, start, end in matches[:6]
                ],
                "values": {"occurrences": len(matches)},
            }
        )
    return output(
        "Observed keywords in selected text.",
        items=items,
        metrics={"sources": len(sources), "method": VERSION},
        warnings=[
            (
                "This English stop-word baseline reports wording. It "
                "does not infer character, intent, emotion, or mental "
                "state; names and topics can dominate counts."
            )
        ],
        outcome="complete" if items else "empty",
    )
