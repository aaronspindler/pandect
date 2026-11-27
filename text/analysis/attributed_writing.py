"""Whole-period writing measurements over bounded reads of attributed originals."""

import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from contextlib import closing
from itertools import islice

from core.analysis.sources import output
from core.evidence.source_clocks import date_only_message_dates
from text.analysis.attributed_sources import source_batches
from text.analysis.deterministic import (
    ABBREVIATIONS,
    SPELLING_VARIANTS,
    VERSION,
    _exclusion_spans,
    _style_examples,
    _usable_ranges,
)
from text.analysis.shared import FUNCTION_WORDS, WORD

MEASUREMENTS = (
    "uppercase_letter_ratio",
    "mean_message_words",
    "mean_sentence_words",
    "punctuation_per_100_words",
    "vocabulary_ratio",
)
EXAMPLES = 6
SENTENCE_BREAK = re.compile(r"[.!?]+(?:\s|$)")
GREETING = re.compile(r"\s*(hi|hey|hello|dear|good morning|good evening)\b", re.I)
SIGN_OFF = re.compile(
    r"\b(regards|sincerely|cheers|bye|goodnight|thanks)[.!\s]*$", re.I
)


def _sample(source):
    exclusions = [
        {"source_id": str(source.id), "start": start, "end": end, "reason": reason}
        for start, end, reason in _exclusion_spans(source, {})
    ]
    ranges = _usable_ranges(source, exclusions)
    parts = []
    cursor = 0
    for start, end in ranges:
        parts.extend((" " * (start - cursor), source.text[start:end]))
        cursor = end
    text = "".join(parts) + " " * (len(source.text) - cursor)
    return text, exclusions, ranges


def _phrases(text, ranges):
    for start, end in ranges:
        previous = []
        for word in WORD.finditer(text, start, end):
            previous.append(word)
            if len(previous) == 3:
                yield (
                    " ".join(match.group().casefold() for match in previous),
                    previous[0].start(),
                    previous[-1].end(),
                )
                del previous[0]


def _add_terms(database, period, kind, counts):
    # A private temporary database keeps vocabulary and phrases exact without
    # holding one Python object per distinct term in the selected corpus.
    database.executemany(
        """
        INSERT INTO terms (period, kind, term, total) VALUES (?, ?, ?, ?)
        ON CONFLICT (period, kind, term) DO UPDATE SET total = total + excluded.total
        """,
        ((period, kind, term, total) for term, total in counts.items()),
    )


class Sample:
    def __init__(self):
        self.selected = self.usable = self.words = self.characters = 0
        self.excluded = self.letters = self.uppercase = self.sentence_words = 0
        self.sentences = self.timed = 0
        self.punctuation = Counter()
        self.symbols = Counter()
        self.first = self.last = None
        self.fallback = None
        self.details = defaultdict(list)

    def add(self, source, text, exclusions, ranges, database, period, *, event_time):
        self.selected += 1
        if self.fallback is None:
            self.fallback = source.id
        self.excluded += len(exclusions)
        for exclusion in exclusions:
            if len(self.details["Excluded sample span"]) >= EXAMPLES:
                break
            self.details["Excluded sample span"].append(
                {
                    **source.anchor(start=exclusion["start"], end=exclusion["end"]),
                    "excerpt": exclusion["reason"],
                }
            )
        if not WORD.search(text):
            return
        self.usable += 1
        self.characters += len(text)
        words = Counter(word.group().casefold() for word in WORD.finditer(text))
        self.words += sum(words.values())
        _add_terms(database, period, "word", words)
        _add_terms(
            database,
            period,
            "phrase",
            Counter(term for term, _, _ in _phrases(text, ranges)),
        )
        for sentence in SENTENCE_BREAK.split(text):
            count = sum(1 for _ in WORD.finditer(sentence))
            if count:
                self.sentences += 1
                self.sentence_words += count
        for character in text:
            if character.isalpha():
                self.letters += 1
                self.uppercase += character.isupper()
            category = unicodedata.category(character)
            if category.startswith("P"):
                self.punctuation[character] += 1
            elif category == "So":
                self.symbols[character] += 1
        if event_time is not None:
            point = (event_time, source.id)
            self.timed += 1
            if self.first is None or event_time < self.first[0]:
                self.first = point
            if self.last is None or event_time > self.last[0]:
                self.last = point
        for label, match in (
            ("Greeting", GREETING.match(text)),
            ("Sign-off", SIGN_OFF.search(text)),
        ):
            if match and len(self.details[label]) < EXAMPLES:
                start, end = match.span(1)
                self.details[label].append(
                    {
                        **source.anchor(start=start, end=end),
                        "excerpt": source.text[start:end],
                    }
                )

    def metrics(self, distinct_words):
        def ratio(numerator, denominator, digits):
            return round(numerator / denominator, digits) if denominator else None

        return {
            "sources_selected": self.selected,
            "sources_usable": self.usable,
            "words": self.words,
            "unique_words": distinct_words,
            "characters": self.characters + max(0, self.usable - 1),
            "excluded_spans": self.excluded,
            "uppercase_letter_ratio": ratio(self.uppercase, self.letters, 4),
            "mean_message_words": ratio(self.words, self.usable, 2),
            "mean_sentence_words": ratio(self.sentence_words, self.sentences, 2),
            "punctuation_per_100_words": ratio(
                100 * self.punctuation.total(), self.words, 2
            ),
            "vocabulary_ratio": ratio(distinct_words, self.words, 4) or 0,
            "method": VERSION,
        }

    def adequate(self, distinct_words):
        return (
            self.words >= 30
            and distinct_words >= 12
            and distinct_words / self.words >= 0.1
        )

    def coverage(self):
        return {
            "sources_with_reported_event_time": self.timed,
            "sources_missing_reported_event_time": self.usable - self.timed,
            "first_reported_event_at": self.first[0].isoformat()
            if self.first
            else None,
            "last_reported_event_at": self.last[0].isoformat() if self.last else None,
            "time_basis": "Reported message event times only.",
            "channels": {"text-message": self.usable} if self.usable else {},
        }


def _term_counts(database, period, kind, *, repeated=False):
    return dict(
        database.execute(
            """
        SELECT term, total FROM terms
        WHERE period = ? AND kind = ? AND total > ? AND LENGTH(term) <= 240
        ORDER BY total DESC, ordinal LIMIT 20
        """,
            (period, kind, int(repeated)),
        )
    )


def _selected_words(database, selected):
    return dict(
        database.execute(
            f"""
        SELECT term, total FROM terms
        WHERE period = 0 AND kind = 'word'
            AND term IN ({",".join("?" for _ in selected)})
        ORDER BY term
        """,
            tuple(selected),
        )
    )


def _item(sample, label, anchors=(), *, text="", values=None):
    anchors = list(anchors)
    return {
        "label": label,
        "text": text,
        "source_ids": list(dict.fromkeys(anchor["source_id"] for anchor in anchors))
        or [sample.fallback],
        "anchors": [
            {key: anchor[key] for key in ("source_id", "start", "end")}
            for anchor in anchors
        ],
        "values": values if values is not None else {},
    }


def _examples(source, text, ranges, label, values, remaining):
    if label == "Repeated three-word phrases":
        if not values:
            return []
        matches = (
            source.anchor(start=start, end=end)
            for term, start, end in _phrases(text, ranges)
            if term in values
        )
        return list(islice(matches, min(remaining, 3)))
    if label == "Capitalization":
        matches = (
            match for match in re.finditer(r"\S", text) if match.group().isupper()
        )
    elif label == "Supplied language context":
        matches = WORD.finditer(text)
    elif label == "Punctuation":
        matches = (
            match for match in re.finditer(r"[^\w\s]", text) if match.group() in values
        )
    else:
        matches = (
            match
            for term in values
            for match in re.finditer(
                re.escape(term)
                if label == "Emoji and other Unicode symbols"
                else r"(?<!\w)" + re.escape(term) + r"(?!\w)",
                text,
                re.I,
            )
        )
    anchors = []
    for match in matches:
        if any(start <= match.start() < match.end() <= end for start, end in ranges):
            anchors.append(source.anchor(start=match.start(), end=match.end()))
            if len(anchors) == min(
                remaining, 3 if label != "Supplied language context" else 1
            ):
                break
    return anchors


def _profile(sample, metrics, adequate, originals, database):
    warnings = [
        "These observations describe writing and cannot establish authorship or "
        "identity.",
        "Message attribution can reflect a shared account. Topic, channel, recipients, "
        "editing and dictation can change measured features.",
        "Symbol counts use Unicode code points and include non-emoji symbols. Spelling "
        "variants are observed forms.",
        "Examples are limited. Counts cover all usable messages in the selection.",
    ]
    if not adequate:
        warnings.append(
            "The usable sample does not meet the descriptive display threshold."
        )
    if not sample.usable:
        return output(
            "The selected messages contain no usable writing.",
            metrics=metrics,
            warnings=warnings,
            outcome="insufficient",
        )

    features = {
        "Punctuation": dict(sample.punctuation.most_common(20)),
        "Capitalization": {
            "uppercase_letters": sample.uppercase,
            "letters": sample.letters,
        },
        "Spelling variants": _selected_words(database, SPELLING_VARIANTS),
        "Vocabulary": _term_counts(database, 0, "word"),
        "Function words": _selected_words(database, FUNCTION_WORDS),
        "Abbreviations": _selected_words(database, ABBREVIATIONS),
        "Emoji and other Unicode symbols": dict(sample.symbols.most_common(20)),
        "Repeated three-word phrases": _term_counts(
            database, 0, "phrase", repeated=True
        ),
        "Supplied language context": {"unknown": sample.usable},
    }
    anchors = defaultdict(list)
    for _, source, text, _, ranges, _ in originals():
        if WORD.search(text):
            for label, values in features.items():
                remaining = EXAMPLES - len(anchors[label])
                if remaining:
                    anchors[label].extend(
                        _examples(source, text, ranges, label, values, remaining)
                    )
    items = [
        _item(
            sample,
            label,
            anchors[label],
            text="Counts describe the selected usable text.",
            values=values,
        )
        for label, values in features.items()
    ]
    items.extend(
        _item(sample, label, [example], text=example["excerpt"])
        for label in ("Greeting", "Sign-off", "Excluded sample span")
        for example in sample.details[label]
    )
    return output(
        "Descriptive writing profile."
        if adequate
        else "The writing sample is too short or repetitive for a descriptive profile.",
        items=items,
        metrics=metrics,
        warnings=warnings,
        outcome="complete" if adequate else "insufficient",
    )


def _example_kind(value, group, earlier, later, adequate):
    if not adequate or earlier is None or later is None or earlier == later:
        return "mixed_or_shared_examples"
    if earlier > later:
        value, earlier, later = -value, -earlier, -later
    if (group == 0 and value > later) or (group == 1 and value < earlier):
        return "contrary_examples"
    if (group == 0 and value <= earlier) or (group == 1 and value >= later):
        return "supporting_examples"
    return "mixed_or_shared_examples"


def _comparison(samples, metrics, adequacy, originals, database):
    adequate = all(adequacy)
    example_types = {
        "supporting_examples": 2,
        "contrary_examples": 2,
        "mixed_or_shared_examples": 1,
    }
    examples = defaultdict(list)
    coverage_anchors = defaultdict(list)
    phrase_counts = {
        term: (earlier, later)
        for term, earlier, later in database.execute(
            """
            SELECT term, SUM(CASE WHEN period = 0 THEN total ELSE 0 END),
                SUM(CASE WHEN period = 1 THEN total ELSE 0 END)
            FROM terms WHERE kind = 'phrase' AND LENGTH(term) <= 240 GROUP BY term
            HAVING SUM(total) >= 2 ORDER BY SUM(total) DESC, term LIMIT 20
            """
        )
    }
    phrase_anchors = defaultdict(list)
    for group, source, text, exclusions, ranges, _ in originals():
        if not WORD.search(text):
            continue
        for feature in MEASUREMENTS:
            if all(
                len(examples[group, feature, kind]) == limit
                for kind, limit in example_types.items()
            ):
                continue
            found = _style_examples(
                ([(source, text)], exclusions), feature, "AB"[group]
            )
            if not found:
                continue
            example = found[0]
            kind = _example_kind(
                example["source_measurement"],
                group,
                metrics[0][feature],
                metrics[1][feature],
                adequate,
            )
            if len(examples[group, feature, kind]) < example_types[kind]:
                example["excerpt"] = example["excerpt"][:240]
                examples[group, feature, kind].append(example)
        if len(coverage_anchors[group]) < 4:
            coverage_anchors[group].extend(
                _style_examples(
                    ([(source, text)], exclusions), "mean_message_words", "AB"[group]
                )
            )
        for term, start, end in _phrases(text, ranges):
            if term in phrase_counts and len(phrase_anchors[group, term]) < 4:
                phrase_anchors[group, term].append(source.anchor(start=start, end=end))

    items, differences = [], []
    for feature in MEASUREMENTS:
        earlier, later = (row[feature] for row in metrics)
        delta = (
            round(later - earlier, 4)
            if adequate and earlier is not None and later is not None
            else None
        )
        if delta:
            differences.append(feature)
        by_kind = {
            kind: examples[0, feature, kind] + examples[1, feature, kind]
            for kind in example_types
        }
        items.append(
            _item(
                samples[0],
                feature.replace("_", " ").capitalize(),
                [example for rows in by_kind.values() for example in rows],
                text="Descriptive difference between the two selected periods.",
                values={
                    "group_a": earlier,
                    "group_b": later,
                    "difference_b_minus_a": delta,
                    **by_kind,
                    "example_basis": (
                        "Selected source-level measurements compared with the full "
                        "period aggregates."
                    ),
                },
            )
        )
    for group, sample in enumerate(samples):
        excluded = sample.details["Excluded sample span"]
        items.append(
            _item(
                sample,
                f"Group {'AB'[group]} coverage",
                coverage_anchors[group] + excluded,
                text="Selected messages, usable text, exclusions and reported times.",
                values=metrics[group]
                | sample.coverage()
                | {"excluded_span_examples": excluded},
            )
        )
    for term, counts in phrase_counts.items():
        items.append(
            _item(
                samples[0],
                "Recurring wording comparison",
                phrase_anchors[0, term] + phrase_anchors[1, term],
                text=term,
                values={
                    "group_a_occurrences": counts[0],
                    "group_b_occurrences": counts[1],
                    "basis": "Case-folded three-word sequences within usable text.",
                },
            )
        )
    interval = None
    earlier, later = samples
    if (
        adequate
        and differences
        and all(sample.timed == sample.usable for sample in samples)
    ):
        before, after = earlier.last, later.first
        if before and after and before[0] < after[0]:
            interval = {
                "start": before[0].isoformat(),
                "end": after[0].isoformat(),
                "differing_features": differences,
                "basis": "Gap between the periods. The actual change time is unknown.",
            }
            items.append(
                {
                    **_item(
                        earlier,
                        "Possible observed-style change interval",
                        text="The selected periods have descriptive differences.",
                        values=interval,
                    ),
                    "source_ids": [before[1], after[1]],
                }
            )
    return output(
        "Compared descriptive writing features."
        if adequate
        else "At least one period is too short or repetitive to assess change.",
        items=items,
        metrics={
            "method": VERSION,
            "group_a_sources": earlier.selected,
            "group_b_sources": later.selected,
            "sources_selected": earlier.selected + later.selected,
            "sources_usable": earlier.usable + later.usable,
            "adequate_groups": sum(adequacy),
            "possible_change_interval": interval,
        },
        warnings=[
            "Period, channel, topic, language, editing and dictation can affect "
            "measured differences. Differences do not prove a change of author.",
            "Examples are limited. Measurements use the usable messages in each "
            "period.",
            "An interval requires two adequate, fully timed, nonoverlapping groups "
            "with observed differences.",
        ],
        outcome="complete" if adequate else "insufficient",
    )


def analyze_attributed_writing(case, run, scope, update_progress):
    samples = [Sample() for _ in range(2 if run.operation == "style_change" else 1)]
    size = len(run.inputs)
    split = scope.get("period_split", size)

    pass_number = 0

    def originals(*, read_times=False):
        nonlocal pass_number
        position = 0
        offset = 5 + pass_number * 45
        pass_number += 1
        for batch in source_batches(case, scope, run.inputs):
            update_progress(offset + 45 * position // size)
            date_only = (
                date_only_message_dates(case, [source.item.pk for source in batch])
                if read_times
                else {}
            )
            for source in batch:
                group = int(position >= split)
                event_time = (
                    source.item.occurred_at
                    if read_times and source.item.pk not in date_only
                    else None
                )
                yield group, source, *_sample(source), event_time
                position += 1

    with closing(sqlite3.connect("")) as database:
        database.execute("PRAGMA cache_size = -4096")
        database.execute(
            """
            CREATE TABLE terms (
                ordinal INTEGER PRIMARY KEY, period INTEGER, kind TEXT,
                term TEXT, total INTEGER, UNIQUE (period, kind, term)
            )
            """
        )
        for group, source, text, exclusions, ranges, event_time in originals(
            read_times=len(samples) == 2
        ):
            samples[group].add(
                source, text, exclusions, ranges, database, group, event_time=event_time
            )
        counts = dict(
            database.execute(
                "SELECT period, COUNT(*) FROM terms WHERE kind = 'word' GROUP BY period"
            )
        )
        metrics = [
            sample.metrics(counts.get(group, 0)) for group, sample in enumerate(samples)
        ]
        adequacy = [
            sample.adequate(counts.get(group, 0))
            for group, sample in enumerate(samples)
        ]
        if len(samples) == 1:
            return _profile(samples[0], metrics[0], adequacy[0], originals, database)
        return _comparison(samples, metrics, adequacy, originals, database)
