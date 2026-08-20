"""Deterministic extraction and writing-style features for message text.

The extractor intentionally has no Django or task-runner dependencies. Evidence
text is accepted as an untrusted value and returned as bounded, JSON-compatible
data that a processing stage can persist without further transformation.
"""

from __future__ import annotations

import ipaddress
import json
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from hashlib import sha256
from statistics import mean, median
from typing import TypedDict

from pandect.processing.extractors.errors import ResourceLimitError

SCHEMA_VERSION = 1
DEFAULT_MAX_TEXT_CHARACTERS = 250_000
MAX_NORMALIZATION_SEGMENTS = 20_000
MAX_INDICATORS_PER_KIND = 200
MAX_INDICATOR_VALUE_CHARACTERS = 2_048
MAX_REPETITION_VALUES = 50
MAX_NGRAM_VALUES = 100
MAX_UNUSUAL_CHARACTER_KINDS = 100
MAX_POSITIONS_PER_UNUSUAL_CHARACTER = 20


class TextSpan(TypedDict):
    """Half-open offsets into normalized and canonical source text."""

    normalized_start: int
    normalized_end: int
    original_start: int
    original_end: int


class NormalizationSegment(TextSpan):
    """One contiguous segment in the normalization map."""

    kind: str


class TextExtraction(TypedDict):
    """Schema-versioned JSON payload suitable for ``ProcessingResult.data``."""

    schema_version: int
    limits: dict[str, int]
    normalization: dict[str, object]
    counts: dict[str, int | float]
    scripts: list[dict[str, object]]
    language_hints: list[dict[str, object]]
    indicators: dict[str, dict[str, object]]
    repeated_content: dict[str, list[dict[str, object]]]
    unusual_characters: list[dict[str, object]]
    readability: dict[str, object]
    lexical_profile: dict[str, object]
    writing_style: dict[str, object]
    ngrams: dict[str, dict[str, list[dict[str, object]]]]


class WritingIdentityVector(TypedDict):
    """A fixed-space local writing observation without source text."""

    feature_space: str
    feature_space_version: int
    dimension: int
    feature_names: list[str]
    vector: list[float]
    word_count: int
    has_signal: bool
    provenance: dict[str, object]


_WORD_RE = re.compile(r"[^\W_]+(?:['\u2019][^\W_]+)*", re.UNICODE)
_TOKEN_RE = re.compile(r"[^\W_]+(?:['\u2019][^\W_]+)*|[^\w\s]", re.UNICODE)
_SENTENCE_RE = re.compile(r"[^.!?\u3002\uff01\uff1f]+(?:[.!?\u3002\uff01\uff1f]+|$)")
_PARAGRAPH_RE = re.compile(r"\n[ \t]*\n+")

_EMAIL_RE = re.compile(
    r"(?<![\w.+-])"
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+"
    r"(?![\w.-])"
)
_URL_RE = re.compile(r"(?i)(?<!\w)(?:https?://|www\.)[^\s<>{}\[\]\"']+")
_DOMAIN_RE = re.compile(
    r"(?i)(?<![\w@-])"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:[a-z]{2,63}|xn--[a-z0-9-]{2,59})"
    r"(?![\w-])"
)
_IPV4_RE = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
_IPV6_RE = re.compile(
    r"(?<![\w:])(?:\[[0-9A-Fa-f:.%]+\]|[0-9A-Fa-f]*:[0-9A-Fa-f:.%:]+)"
    r"(?![\w:])"
)
_MAC_RE = re.compile(
    r"(?i)(?<![0-9a-f])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}"
    r"(?![0-9a-f])|(?<![0-9a-f])(?:[0-9a-f]{4}\.){2}[0-9a-f]{4}"
    r"(?![0-9a-f])"
)
_HANDLE_RE = re.compile(r"(?<![\w@])@[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}")
_HASHTAG_RE = re.compile(r"(?<![\w#])#[\w][\w-]{0,99}", re.UNICODE)
_COORDINATE_RE = re.compile(
    r"(?<![\d.])"
    r"(?P<latitude>[+-]?(?:[0-8]?\d(?:\.\d+)?|90(?:\.0+)?))"
    r"\s*[,;/]\s*"
    r"(?P<longitude>[+-]?(?:1[0-7]\d(?:\.\d+)?|"
    r"\d?\d(?:\.\d+)?|180(?:\.0+)?))"
    r"(?!\d)(?!\.\d)"
)
_HASH_RE = re.compile(
    r"(?i)(?<![0-9a-f])(?:[0-9a-f]{128}|[0-9a-f]{64}|"
    r"[0-9a-f]{40}|[0-9a-f]{32})(?![0-9a-f])"
)
_UUID_RE = re.compile(
    r"(?i)(?<![0-9a-f])"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}(?![0-9a-f])"
)
_LABELLED_ID_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}[-_][A-Z0-9]{3,32}\b")
_CURRENCY_RE = re.compile(
    r"(?i)(?<!\w)(?:[$\u00a3\u20ac\u00a5\u20b9]\s?"
    r"(?:\d{1,3}(?:[ ,.]\d{3})*|\d+)(?:[.,]\d{1,2})?"
    r"|(?:USD|CAD|EUR|GBP|JPY|CNY|INR|AUD|NZD)\s?"
    r"(?:\d{1,3}(?:[ ,.]\d{3})*|\d+)(?:[.,]\d{1,2})?"
    r"|(?:\d{1,3}(?:[ ,.]\d{3})*|\d+)(?:[.,]\d{1,2})?\s?"
    r"(?:USD|CAD|EUR|GBP|JPY|CNY|INR|AUD|NZD))(?!\w)"
)
_DATE_RE = re.compile(
    r"(?ix)(?<!\w)(?:"
    r"\d{4}[-/.](?:0?[1-9]|1[0-2])[-/.](?:0?[1-9]|[12]\d|3[01])"
    r"|(?:0?[1-9]|[12]\d|3[01])[-/.](?:0?[1-9]|1[0-2])[-/.]\d{2,4}"
    r"|(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)\s+(?:0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?(?:,\s*|\s+)"
    r"\d{4}"
    r"|(?:0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?\s+"
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)\s+\d{4})"
    r"(?!\w)"
)
_TIME_RE = re.compile(
    r"(?ix)(?<!\d)(?:"
    r"(?:[01]?\d|2[0-3]):[0-5]\d(?:"
    r":[0-5]\d(?:\.\d{1,6})?)?(?:\s?(?:z|[+-][0-2]\d:?[0-5]\d|"
    r"[ap]\.?m\.?))?"
    r"|(?:1[0-2]|0?[1-9])(?::[0-5]\d)?\s*[ap]\.?m\.?)"
    r"(?!\d)"
)
_PHONE_RE = re.compile(r"(?<!\w)\+?\d(?:[\d(). -]{5,}\d)(?!\w)")
_DOUBLE_QUOTE_RE = re.compile(r'"(?:\\.|[^"\\]){1,10000}"')
_CURLY_QUOTE_RE = re.compile(r"\u201c[^\u201d\n]{1,10000}\u201d")
_GUILLEMET_RE = re.compile(r"\u00ab[^\u00bb\n]{1,10000}\u00bb")

_FUNCTION_WORDS = frozenset(
    "a about after all also am an and any are as at be because been before being "
    "between both but by can could did do does doing down during each few for from "
    "further had has have having he her here hers herself him himself his how i if "
    "in into is it its itself just me more most my myself no nor not of off on once "
    "only or other our ours ourselves out over own same she should so some such than "
    "that the their theirs them themselves then there these they this those through "
    "to too under until up very was we were what when where which while who whom why "
    "will with would you your yours yourself yourselves".split()
)
_LANGUAGE_STOPWORDS = {
    "en": frozenset(
        "the and that have for not with you this but from they are was".split()
    ),
    "es": frozenset("el la los las de que y en un una por con no para es".split()),
    "fr": frozenset("le la les de des et que en un une pour avec pas est".split()),
    "de": frozenset("der die das den dem und ist ein eine nicht mit von zu".split()),
}
_GREETING_PATTERNS = (
    re.compile(r"(?i)^(?:hi|hello|hey|dear|good (?:morning|afternoon|evening))\b"),
    re.compile(r"(?i)^(?:greetings|to whom it may concern)\b"),
)
_SIGNOFF_PATTERNS = (
    re.compile(r"(?i)^(?:thanks|thank you|regards|kind regards|best|best wishes)\b"),
    re.compile(r"(?i)^(?:sincerely|cheers|yours truly|sent from my)\b"),
)
_AMERICAN_BRITISH_PAIRS = (
    ("color", "colour"),
    ("favorite", "favourite"),
    ("honor", "honour"),
    ("organize", "organise"),
    ("organization", "organisation"),
    ("center", "centre"),
    ("theater", "theatre"),
    ("analyze", "analyse"),
    ("defense", "defence"),
    ("license", "licence"),
    ("traveled", "travelled"),
)

WRITING_IDENTITY_FEATURE_SPACE = "pandect.writing-identity.v1"
WRITING_IDENTITY_FEATURE_SPACE_VERSION = 1
WRITING_IDENTITY_PROCESSOR_NAME = "pandect.writing-identity"
WRITING_IDENTITY_PROCESSOR_VERSION = "1"
WRITING_IDENTITY_PREPROCESSING_VERSION = "text-profile-v1/writing-vector-v1"
WRITING_IDENTITY_HASH_BINS = 32

_WRITING_SCALAR_FEATURE_NAMES = (
    "lexical:type_token_ratio",
    "lexical:root_type_token_ratio_squashed",
    "lexical:hapax_fraction",
    "lexical:dis_legomena_fraction",
    "lexical:function_word_fraction",
    "readability:applicable",
    "readability:latin_lexical_coverage",
    "readability:flesch_reading_ease_squashed",
    "readability:flesch_kincaid_grade_squashed",
    "readability:words_per_sentence_squashed",
    "readability:syllables_per_word_squashed",
    "length:mean_word_characters_squashed",
    "length:median_word_characters_squashed",
    "length:mean_sentence_words_squashed",
    "length:median_sentence_words_squashed",
    "capitalization:uppercase_letter_fraction",
    "capitalization:uppercase_word_fraction",
    "capitalization:titlecase_word_fraction",
    "spacing:space_rate",
    "spacing:tab_rate",
    "spacing:newline_rate",
    "spacing:multiple_space_run_rate",
    "spacing:leading_space_line_fraction",
    "spacing:trailing_space_line_fraction",
    "markers:emoji_per_word",
    "markers:contraction_per_word",
    "markers:abbreviation_per_word",
    "markers:greeting_present",
    "markers:signoff_present",
    "spelling:american_variant_fraction",
    "spelling:british_variant_fraction",
)
_WRITING_PUNCTUATION_FEATURES = (
    ".",
    ",",
    "!",
    "?",
    ";",
    ":",
    "'",
    "\u2019",
    '"',
    "\u201c",
    "\u201d",
    "-",
    "\u2013",
    "\u2014",
    "\u2026",
    "(",
    ")",
    "/",
)
_WRITING_FUNCTION_WORD_FEATURES = tuple(sorted(_FUNCTION_WORDS))
_WRITING_NGRAM_FAMILIES = (
    ("characters", "3"),
    ("characters", "4"),
    ("words", "1"),
    ("words", "2"),
    ("words", "3"),
)


def _writing_identity_feature_names() -> tuple[str, ...]:
    names = list(_WRITING_SCALAR_FEATURE_NAMES)
    names.extend(f"punctuation:{value}" for value in _WRITING_PUNCTUATION_FEATURES)
    names.extend(f"function_word:{value}" for value in _WRITING_FUNCTION_WORD_FEATURES)
    for category, size in _WRITING_NGRAM_FAMILIES:
        names.extend(
            f"hashed_{category}_{size}:{index:02d}"
            for index in range(WRITING_IDENTITY_HASH_BINS)
        )
    return tuple(names)


WRITING_IDENTITY_FEATURE_NAMES = _writing_identity_feature_names()
WRITING_IDENTITY_DIMENSION = len(WRITING_IDENTITY_FEATURE_NAMES)
WRITING_IDENTITY_FEATURE_DEFINITION_SHA256 = sha256(
    "\n".join(
        f"{index}:{name}" for index, name in enumerate(WRITING_IDENTITY_FEATURE_NAMES)
    ).encode("utf-8")
).hexdigest()


def extract_message_text(
    text: str,
    *,
    max_characters: int = DEFAULT_MAX_TEXT_CHARACTERS,
) -> TextExtraction:
    """Return deterministic text facts and writing features.

    ``ResourceLimitError`` is deliberately content-free so a failed task can
    safely expose its code without leaking evidence into logs.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if max_characters < 1:
        raise ValueError("max_characters must be positive")
    if len(text) > max_characters:
        raise ResourceLimitError()

    normalized, span_map = _normalize_with_spans(text)
    words = [match.group(0) for match in _WORD_RE.finditer(normalized)]
    folded_words = [word.casefold().replace("\u2019", "'") for word in words]
    sentences = _sentences(normalized)
    indicators = _extract_indicators(normalized, span_map)
    counts = _counts(text, normalized, words, sentences)

    return {
        "schema_version": SCHEMA_VERSION,
        "limits": {
            "max_text_characters": max_characters,
            "max_normalization_segments": MAX_NORMALIZATION_SEGMENTS,
            "max_indicators_per_kind": MAX_INDICATORS_PER_KIND,
            "max_ngram_values_per_size": MAX_NGRAM_VALUES,
            "max_repetition_values_per_kind": MAX_REPETITION_VALUES,
        },
        "normalization": {
            "form": "NFC",
            "newline_policy": "CRLF and CR converted to LF",
            "offset_unit": "Unicode code points; half-open spans",
            "text": normalized,
            "original_length": len(text),
            "normalized_length": len(normalized),
            "changed": normalized != text,
            "span_map": span_map,
        },
        "counts": counts,
        "scripts": _script_distribution(normalized),
        "language_hints": _language_hints(folded_words, normalized),
        "indicators": indicators,
        "repeated_content": _repeated_content(normalized, sentences, folded_words),
        "unusual_characters": _unusual_characters(normalized, span_map),
        "readability": _readability(words, sentences),
        "lexical_profile": _lexical_profile(folded_words),
        "writing_style": _writing_style(normalized, words, folded_words, sentences),
        "ngrams": _ngrams(normalized, folded_words),
    }


def build_writing_identity_vector(
    profile: TextExtraction,
) -> WritingIdentityVector:
    """Build a stable local authorship feature vector from a text profile.

    The returned feature names are a public, fixed vocabulary. Observed words,
    punctuation strings outside that vocabulary, and n-gram values are never
    returned. N-grams enter fixed signed SHA-256 buckets, so this payload can be
    persisted as an opaque identity observation without disclosing source text.
    """
    if not isinstance(profile, dict) or profile.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported text extraction schema")

    counts = _as_mapping(profile.get("counts"))
    lexical = _as_mapping(profile.get("lexical_profile"))
    readability = _as_mapping(profile.get("readability"))
    style = _as_mapping(profile.get("writing_style"))
    ngrams = _as_mapping(profile.get("ngrams"))
    word_count = max(0, int(_number(counts, "words")))

    scalar_values = _writing_scalar_values(
        counts=counts,
        lexical=lexical,
        readability=readability,
        style=style,
        word_count=word_count,
    )
    punctuation_values = _writing_punctuation_values(
        style=style,
        character_count=max(0, int(_number(counts, "normalized_characters"))),
    )
    function_word_values = _writing_function_word_values(
        style=style,
        word_count=word_count,
    )
    families = [
        _l2_normalize(scalar_values),
        _l2_normalize(punctuation_values),
        _l2_normalize(function_word_values),
    ]
    for category, size in _WRITING_NGRAM_FAMILIES:
        families.append(_hashed_ngram_family(ngrams, category, size))

    vector = _l2_normalize([component for family in families for component in family])
    if len(vector) != WRITING_IDENTITY_DIMENSION:
        raise RuntimeError("writing identity feature definition is inconsistent")

    feature_payload = {
        "schema_version": profile["schema_version"],
        "counts": profile.get("counts", {}),
        "readability": profile.get("readability", {}),
        "lexical_profile": profile.get("lexical_profile", {}),
        "writing_style": profile.get("writing_style", {}),
        "ngrams": profile.get("ngrams", {}),
    }
    input_profile_sha256 = _json_sha256(feature_payload)
    has_signal = word_count > 0 and any(component != 0.0 for component in vector)
    return {
        "feature_space": WRITING_IDENTITY_FEATURE_SPACE,
        "feature_space_version": WRITING_IDENTITY_FEATURE_SPACE_VERSION,
        "dimension": WRITING_IDENTITY_DIMENSION,
        "feature_names": list(WRITING_IDENTITY_FEATURE_NAMES),
        "vector": vector,
        "word_count": word_count,
        "has_signal": has_signal,
        "provenance": {
            "processor_name": WRITING_IDENTITY_PROCESSOR_NAME,
            "processor_version": WRITING_IDENTITY_PROCESSOR_VERSION,
            "preprocessing_version": WRITING_IDENTITY_PREPROCESSING_VERSION,
            "input_profile_schema_version": SCHEMA_VERSION,
            "input_profile_sha256": input_profile_sha256,
            "feature_definition_sha256": (WRITING_IDENTITY_FEATURE_DEFINITION_SHA256),
            "normalization": "per-family L2 followed by global L2",
            "ngram_hashing": "signed SHA-256 into 32 bins per family",
            "source_text_included": False,
        },
    }


def _writing_scalar_values(
    *,
    counts: Mapping[str, object],
    lexical: Mapping[str, object],
    readability: Mapping[str, object],
    style: Mapping[str, object],
    word_count: int,
) -> list[float]:
    distributions = _as_mapping(style.get("distributions"))
    word_lengths = _as_mapping(distributions.get("word_length_characters"))
    sentence_lengths = _as_mapping(distributions.get("sentence_length_words_heuristic"))
    capitalization = _as_mapping(style.get("capitalization"))
    spacing = _as_mapping(style.get("spacing"))
    character_count = max(0, int(_number(counts, "normalized_characters")))
    line_count = max(0, int(_number(counts, "lines")))

    spelling_variants = _as_list(style.get("spelling_variants"))
    american_count = 0.0
    british_count = 0.0
    for value in spelling_variants:
        entry = _as_mapping(value)
        american_count += max(0.0, _number(entry, "american_count"))
        british_count += max(0.0, _number(entry, "british_count"))
    spelling_total = american_count + british_count

    emoji_count = _counter_total(style.get("emoji_candidates"))
    contraction_count = _counter_total(style.get("contractions"))
    abbreviation_count = _counter_total(style.get("abbreviations"))
    titlecase_words = max(0.0, _number(capitalization, "titlecase_words"))

    values_by_name = {
        "lexical:type_token_ratio": _unit(_number(lexical, "type_token_ratio")),
        "lexical:root_type_token_ratio_squashed": _squash(
            _number(lexical, "root_type_token_ratio"), 20.0
        ),
        "lexical:hapax_fraction": _rate(_number(lexical, "hapax_words"), word_count),
        "lexical:dis_legomena_fraction": _rate(
            _number(lexical, "dis_legomena_words"), word_count
        ),
        "lexical:function_word_fraction": _rate(
            _number(lexical, "function_word_tokens"), word_count
        ),
        "readability:applicable": 1.0 if readability.get("applicable") is True else 0.0,
        "readability:latin_lexical_coverage": _unit(
            _number(readability, "latin_lexical_coverage")
        ),
        "readability:flesch_reading_ease_squashed": _squash(
            _number(readability, "flesch_reading_ease"), 100.0
        ),
        "readability:flesch_kincaid_grade_squashed": _squash(
            _number(readability, "flesch_kincaid_grade"), 20.0
        ),
        "readability:words_per_sentence_squashed": _squash(
            _number(readability, "words_per_sentence"), 30.0
        ),
        "readability:syllables_per_word_squashed": _squash(
            _number(readability, "syllables_per_word"), 3.0
        ),
        "length:mean_word_characters_squashed": _squash(
            _number(word_lengths, "mean"), 10.0
        ),
        "length:median_word_characters_squashed": _squash(
            _number(word_lengths, "median"), 10.0
        ),
        "length:mean_sentence_words_squashed": _squash(
            _number(sentence_lengths, "mean"), 50.0
        ),
        "length:median_sentence_words_squashed": _squash(
            _number(sentence_lengths, "median"), 50.0
        ),
        "capitalization:uppercase_letter_fraction": _unit(
            _number(capitalization, "uppercase_letter_fraction")
        ),
        "capitalization:uppercase_word_fraction": _unit(
            _number(capitalization, "all_uppercase_word_fraction")
        ),
        "capitalization:titlecase_word_fraction": _rate(titlecase_words, word_count),
        "spacing:space_rate": _rate(
            _number(spacing, "spaces"), character_count, scale=10.0
        ),
        "spacing:tab_rate": _rate(
            _number(spacing, "tabs"), character_count, scale=100.0
        ),
        "spacing:newline_rate": _rate(
            _number(spacing, "newlines"), character_count, scale=50.0
        ),
        "spacing:multiple_space_run_rate": _rate(
            _number(spacing, "multiple_space_runs"),
            character_count,
            scale=50.0,
        ),
        "spacing:leading_space_line_fraction": _rate(
            _number(spacing, "leading_space_lines"), line_count
        ),
        "spacing:trailing_space_line_fraction": _rate(
            _number(spacing, "trailing_space_lines"), line_count
        ),
        "markers:emoji_per_word": _rate(emoji_count, word_count, scale=5.0),
        "markers:contraction_per_word": _rate(contraction_count, word_count, scale=5.0),
        "markers:abbreviation_per_word": _rate(
            abbreviation_count, word_count, scale=5.0
        ),
        "markers:greeting_present": 1.0 if style.get("greeting") else 0.0,
        "markers:signoff_present": 1.0 if style.get("signoff") else 0.0,
        "spelling:american_variant_fraction": _rate(american_count, spelling_total),
        "spelling:british_variant_fraction": _rate(british_count, spelling_total),
    }
    return [_finite(values_by_name[name]) for name in _WRITING_SCALAR_FEATURE_NAMES]


def _writing_punctuation_values(
    *, style: Mapping[str, object], character_count: int
) -> list[float]:
    observed = _counter_map(style.get("punctuation"))
    return [
        _rate(observed.get(value, 0.0), character_count, scale=50.0)
        for value in _WRITING_PUNCTUATION_FEATURES
    ]


def _writing_function_word_values(
    *, style: Mapping[str, object], word_count: int
) -> list[float]:
    function_words = _as_mapping(style.get("function_words"))
    observed = _counter_map(function_words.get("values"))
    return [
        _rate(observed.get(value, 0.0), word_count, scale=5.0)
        for value in _WRITING_FUNCTION_WORD_FEATURES
    ]


def _hashed_ngram_family(
    ngrams: Mapping[str, object], category: str, size: str
) -> list[float]:
    category_values = _as_mapping(ngrams.get(category))
    entries = _as_list(category_values.get(size))
    buckets = [0.0] * WRITING_IDENTITY_HASH_BINS
    for raw_entry in entries:
        entry = _as_mapping(raw_entry)
        count = max(0.0, _number(entry, "count"))
        if count == 0.0:
            continue
        value = entry.get("value")
        if not isinstance(value, str):
            continue
        if entry.get("value_truncated") is True and isinstance(
            entry.get("sha256"), str
        ):
            value = f"sha256:{entry['sha256']}"
        digest = sha256(f"{category}:{size}:{value}".encode()).digest()
        index = int.from_bytes(digest[:8], "big") % WRITING_IDENTITY_HASH_BINS
        sign = 1.0 if digest[8] & 1 else -1.0
        buckets[index] += sign * math.log1p(count)
    return _l2_normalize(buckets)


def _counter_map(value: object) -> dict[str, float]:
    counts: dict[str, float] = {}
    for raw_entry in _as_list(value):
        entry = _as_mapping(raw_entry)
        name = entry.get("value")
        if not isinstance(name, str):
            continue
        counts[name] = counts.get(name, 0.0) + max(0.0, _number(entry, "count"))
    return counts


def _counter_total(value: object) -> float:
    return sum(_counter_map(value).values())


def _as_mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _number(values: Mapping[str, object], key: str) -> float:
    value = values.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return float(value) if math.isfinite(value) else 0.0


def _rate(numerator: float, denominator: float | int, *, scale: float = 1.0) -> float:
    if denominator <= 0:
        return 0.0
    return _unit(numerator / denominator * scale)


def _unit(value: float) -> float:
    return max(-1.0, min(1.0, _finite(value)))


def _squash(value: float, scale: float) -> float:
    return _finite(math.tanh(_finite(value) / scale))


def _finite(value: float) -> float:
    return float(value) if math.isfinite(value) else 0.0


def _l2_normalize(values: Sequence[float]) -> list[float]:
    finite_values = [_finite(value) for value in values]
    magnitude = math.sqrt(sum(value * value for value in finite_values))
    if magnitude == 0.0:
        return [0.0] * len(finite_values)
    return [round(value / magnitude, 12) for value in finite_values]


def _json_sha256(value: object) -> str:
    try:
        serialized = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("text extraction profile is not JSON-compatible") from error
    return sha256(serialized.encode("utf-8")).hexdigest()


def _normalize_with_spans(text: str) -> tuple[str, list[NormalizationSegment]]:
    units = list(_newline_units(text))
    clusters: list[list[tuple[str, int, int, str]]] = []
    current: list[tuple[str, int, int, str]] = []

    for unit in units:
        character = unit[0]
        if current and not _joins_normalization_cluster(current, character):
            clusters.append(current)
            current = []
        current.append(unit)
    if current:
        clusters.append(current)

    output: list[str] = []
    segments: list[NormalizationSegment] = []
    normalized_offset = 0
    for cluster in clusters:
        pre_normalized = "".join(unit[0] for unit in cluster)
        normalized_cluster = unicodedata.normalize("NFC", pre_normalized)
        original_start = cluster[0][1]
        original_end = cluster[-1][2]
        normalized_start = normalized_offset
        normalized_offset += len(normalized_cluster)
        output.append(normalized_cluster)

        if any(unit[3] == "newline" for unit in cluster):
            kind = "newline"
        elif normalized_cluster != pre_normalized:
            kind = "unicode_nfc"
        else:
            kind = "identity"
        segment: NormalizationSegment = {
            "normalized_start": normalized_start,
            "normalized_end": normalized_offset,
            "original_start": original_start,
            "original_end": original_end,
            "kind": kind,
        }
        _append_segment(segments, segment)
        if len(segments) > MAX_NORMALIZATION_SEGMENTS:
            raise ResourceLimitError()

    return "".join(output), segments


def _newline_units(text: str) -> Iterator[tuple[str, int, int, str]]:
    index = 0
    while index < len(text):
        if text[index] == "\r":
            end = index + 2 if text[index : index + 2] == "\r\n" else index + 1
            yield "\n", index, end, "newline"
            index = end
            continue
        yield text[index], index, index + 1, "identity"
        index += 1


def _joins_normalization_cluster(
    current: Sequence[tuple[str, int, int, str]],
    character: str,
) -> bool:
    if character == "\n" or current[-1][0] == "\n":
        return False
    if unicodedata.category(character).startswith("M"):
        return True
    previous = current[-1][0]
    return _hangul_joins(previous, character)


def _hangul_joins(previous: str, character: str) -> bool:
    previous_code = ord(previous)
    code = ord(character)
    previous_is_l = 0x1100 <= previous_code <= 0x1112 or (
        0xA960 <= previous_code <= 0xA97C
    )
    current_is_v = 0x1161 <= code <= 0x1175 or 0xD7B0 <= code <= 0xD7C6
    previous_is_v = 0x1161 <= previous_code <= 0x1175 or (
        0xD7B0 <= previous_code <= 0xD7C6
    )
    current_is_t = 0x11A8 <= code <= 0x11C2 or 0xD7CB <= code <= 0xD7FB
    previous_is_lv = 0xAC00 <= previous_code <= 0xD7A3 and (
        (previous_code - 0xAC00) % 28 == 0
    )
    return (previous_is_l and current_is_v) or (
        (previous_is_v or previous_is_lv) and current_is_t
    )


def _append_segment(
    segments: list[NormalizationSegment], segment: NormalizationSegment
) -> None:
    if not segments or segment["kind"] != "identity":
        segments.append(segment)
        return
    previous = segments[-1]
    if (
        previous["kind"] == "identity"
        and previous["normalized_end"] == segment["normalized_start"]
        and previous["original_end"] == segment["original_start"]
    ):
        previous["normalized_end"] = segment["normalized_end"]
        previous["original_end"] = segment["original_end"]
        return
    segments.append(segment)


def _source_span(
    start: int, end: int, span_map: Sequence[NormalizationSegment]
) -> TextSpan:
    intersecting = [
        segment
        for segment in span_map
        if segment["normalized_end"] > start and segment["normalized_start"] < end
    ]
    if not intersecting:
        return {
            "normalized_start": start,
            "normalized_end": end,
            "original_start": start,
            "original_end": end,
        }

    first = intersecting[0]
    last = intersecting[-1]
    original_start = _map_boundary(start, first, is_end=False)
    original_end = _map_boundary(end, last, is_end=True)
    return {
        "normalized_start": start,
        "normalized_end": end,
        "original_start": original_start,
        "original_end": original_end,
    }


def _map_boundary(offset: int, segment: NormalizationSegment, *, is_end: bool) -> int:
    normalized_length = segment["normalized_end"] - segment["normalized_start"]
    original_length = segment["original_end"] - segment["original_start"]
    if segment["kind"] == "identity" and normalized_length == original_length:
        return segment["original_start"] + offset - segment["normalized_start"]
    if offset <= segment["normalized_start"]:
        return segment["original_start"]
    if offset >= segment["normalized_end"]:
        return segment["original_end"]
    return segment["original_end"] if is_end else segment["original_start"]


def _extract_indicators(
    text: str, span_map: Sequence[NormalizationSegment]
) -> dict[str, dict[str, object]]:
    email_spans = [(match.start(), match.end()) for match in _EMAIL_RE.finditer(text)]
    date_spans = [(match.start(), match.end()) for match in _DATE_RE.finditer(text)]
    time_spans = [(match.start(), match.end()) for match in _TIME_RE.finditer(text)]
    mac_spans = [(match.start(), match.end()) for match in _MAC_RE.finditer(text)]
    ipv4_spans = [(match.start(), match.end()) for match in _IPV4_RE.finditer(text)]
    coordinate_spans = [
        (match.start(), match.end()) for match in _COORDINATE_RE.finditer(text)
    ]

    extractors: dict[str, Callable[[], Iterable[dict[str, object]]]] = {
        "urls": lambda: _regex_indicators(_URL_RE, text, span_map, trim_url=True),
        "email_addresses": lambda: _regex_indicators(_EMAIL_RE, text, span_map),
        "domains": lambda: _domain_indicators(text, span_map),
        "ipv4_addresses": lambda: _ip_indicators(_IPV4_RE, text, span_map, version=4),
        "ipv6_addresses": lambda: _ip_indicators(
            _IPV6_RE,
            text,
            span_map,
            version=6,
            excluded_spans=mac_spans,
        ),
        "phone_candidates": lambda: _phone_indicators(
            text,
            span_map,
            excluded_spans=(
                email_spans
                + date_spans
                + time_spans
                + mac_spans
                + ipv4_spans
                + coordinate_spans
            ),
        ),
        "handles": lambda: _regex_indicators(
            _HANDLE_RE,
            text,
            span_map,
            excluded_spans=email_spans,
        ),
        "hashtags": lambda: _regex_indicators(_HASHTAG_RE, text, span_map),
        "coordinate_candidates": lambda: _coordinate_indicators(text, span_map),
        "cryptographic_hashes": lambda: _hash_indicators(text, span_map),
        "mac_addresses": lambda: _mac_indicators(text, span_map),
        "identifiers": lambda: _identifier_indicators(text, span_map),
        "currency_candidates": lambda: _regex_indicators(_CURRENCY_RE, text, span_map),
        "date_candidates": lambda: _date_indicators(text, span_map),
        "time_candidates": lambda: _regex_indicators(
            _TIME_RE, text, span_map, excluded_spans=mac_spans
        ),
        "quoted_passages": lambda: _quoted_indicators(text, span_map),
    }
    return {
        kind: _bounded_indicators(factory()) for kind, factory in extractors.items()
    }


def _bounded_indicators(
    indicators: Iterable[dict[str, object]],
) -> dict[str, object]:
    unique: list[dict[str, object]] = []
    seen: set[tuple[object, ...]] = set()
    total = 0
    for indicator in indicators:
        span = indicator["span"]
        assert isinstance(span, dict)
        key = (
            span["normalized_start"],
            span["normalized_end"],
            indicator.get("kind"),
        )
        if key in seen:
            continue
        seen.add(key)
        total += 1
        if len(unique) < MAX_INDICATORS_PER_KIND:
            unique.append(indicator)
    return {
        "total": total,
        "returned": len(unique),
        "truncated": total > len(unique),
        "values": unique,
    }


def _regex_indicators(
    pattern: re.Pattern[str],
    text: str,
    span_map: Sequence[NormalizationSegment],
    *,
    excluded_spans: Sequence[tuple[int, int]] = (),
    trim_url: bool = False,
) -> Iterator[dict[str, object]]:
    for match in pattern.finditer(text):
        start, end = match.span()
        if trim_url:
            end = _trim_url_end(text, start, end)
        if end <= start or _overlaps_any(start, end, excluded_spans):
            continue
        yield _indicator(text, start, end, span_map)


def _indicator(
    text: str,
    start: int,
    end: int,
    span_map: Sequence[NormalizationSegment],
    *,
    kind: str | None = None,
    attributes: dict[str, object] | None = None,
    normalized_value: str | None = None,
) -> dict[str, object]:
    value = text[start:end]
    value_truncated = len(value) > MAX_INDICATOR_VALUE_CHARACTERS
    result: dict[str, object] = {
        "value": value[:MAX_INDICATOR_VALUE_CHARACTERS],
        "value_truncated": value_truncated,
        "span": _source_span(start, end, span_map),
    }
    if kind is not None:
        result["kind"] = kind
    if attributes:
        result["attributes"] = attributes
    if normalized_value is not None:
        result["normalized_value"] = normalized_value
    return result


def _trim_url_end(text: str, start: int, end: int) -> int:
    while end > start and text[end - 1] in ".,;:!?":
        end -= 1
    bracket_pairs = (("(", ")"), ("[", "]"), ("{", "}"))
    for opening, closing in bracket_pairs:
        while end > start and text[end - 1] == closing:
            value = text[start:end]
            if value.count(closing) <= value.count(opening):
                break
            end -= 1
    return end


def _domain_indicators(
    text: str, span_map: Sequence[NormalizationSegment]
) -> Iterator[dict[str, object]]:
    for match in _DOMAIN_RE.finditer(text):
        value = match.group(0)
        try:
            ipaddress.ip_address(value)
        except ValueError:
            yield _indicator(
                text,
                match.start(),
                match.end(),
                span_map,
                kind="domain_candidate",
                normalized_value=value.casefold(),
            )
    for email_match in _EMAIL_RE.finditer(text):
        email = email_match.group(0)
        domain = email.rsplit("@", maxsplit=1)[1]
        start = email_match.end() - len(domain)
        yield _indicator(
            text,
            start,
            email_match.end(),
            span_map,
            kind="email_domain",
            normalized_value=domain.casefold(),
        )


def _ip_indicators(
    pattern: re.Pattern[str],
    text: str,
    span_map: Sequence[NormalizationSegment],
    *,
    version: int,
    excluded_spans: Sequence[tuple[int, int]] = (),
) -> Iterator[dict[str, object]]:
    for match in pattern.finditer(text):
        start, end = match.span()
        if _overlaps_any(start, end, excluded_spans):
            continue
        candidate = match.group(0).strip("[]")
        if version == 6 and candidate.count(":") < 2:
            continue
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if address.version != version:
            continue
        yield _indicator(
            text,
            start,
            end,
            span_map,
            normalized_value=address.compressed,
            attributes={
                "version": version,
                "is_private": address.is_private,
                "is_global": address.is_global,
                "is_loopback": address.is_loopback,
                "is_multicast": address.is_multicast,
            },
        )


def _phone_indicators(
    text: str,
    span_map: Sequence[NormalizationSegment],
    *,
    excluded_spans: Sequence[tuple[int, int]],
) -> Iterator[dict[str, object]]:
    for match in _PHONE_RE.finditer(text):
        start, end = match.span()
        if _overlaps_any(start, end, excluded_spans):
            continue
        digits = "".join(
            character for character in match.group(0) if character.isdigit()
        )
        if not 7 <= len(digits) <= 15:
            continue
        normalized = f"+{digits}" if match.group(0).lstrip().startswith("+") else digits
        yield _indicator(
            text,
            start,
            end,
            span_map,
            normalized_value=normalized,
            attributes={"digit_count": len(digits), "candidate_only": True},
        )


def _coordinate_indicators(
    text: str, span_map: Sequence[NormalizationSegment]
) -> Iterator[dict[str, object]]:
    for match in _COORDINATE_RE.finditer(text):
        latitude = float(match.group("latitude"))
        longitude = float(match.group("longitude"))
        yield _indicator(
            text,
            match.start(),
            match.end(),
            span_map,
            normalized_value=f"{latitude:.8f},{longitude:.8f}",
            attributes={
                "latitude": latitude,
                "longitude": longitude,
                "candidate_only": True,
            },
        )


def _hash_indicators(
    text: str, span_map: Sequence[NormalizationSegment]
) -> Iterator[dict[str, object]]:
    algorithms = {
        32: "md5_candidate",
        40: "sha1_candidate",
        64: "sha256_candidate",
        128: "sha512_candidate",
    }
    for match in _HASH_RE.finditer(text):
        value = match.group(0)
        yield _indicator(
            text,
            match.start(),
            match.end(),
            span_map,
            kind=algorithms[len(value)],
            normalized_value=value.casefold(),
        )


def _mac_indicators(
    text: str, span_map: Sequence[NormalizationSegment]
) -> Iterator[dict[str, object]]:
    for match in _MAC_RE.finditer(text):
        value = match.group(0)
        compact = re.sub(r"[-:.]", "", value).casefold()
        normalized = ":".join(
            compact[index : index + 2] for index in range(0, len(compact), 2)
        )
        yield _indicator(
            text,
            match.start(),
            match.end(),
            span_map,
            normalized_value=normalized,
        )


def _identifier_indicators(
    text: str, span_map: Sequence[NormalizationSegment]
) -> Iterator[dict[str, object]]:
    for match in _UUID_RE.finditer(text):
        yield _indicator(
            text,
            match.start(),
            match.end(),
            span_map,
            kind="uuid",
            normalized_value=match.group(0).casefold(),
        )
    for match in _LABELLED_ID_RE.finditer(text):
        yield _indicator(
            text,
            match.start(),
            match.end(),
            span_map,
            kind="labelled_identifier",
        )


def _date_indicators(
    text: str, span_map: Sequence[NormalizationSegment]
) -> Iterator[dict[str, object]]:
    for match in _DATE_RE.finditer(text):
        value = match.group(0)
        if re.fullmatch(r"\d{4}[-/.].*", value):
            format_hint = "year_first"
        elif re.search(r"[A-Za-z]", value):
            format_hint = "named_month"
        else:
            format_hint = "ambiguous_day_month_order"
        yield _indicator(
            text,
            match.start(),
            match.end(),
            span_map,
            attributes={"format_hint": format_hint, "candidate_only": True},
        )


def _quoted_indicators(
    text: str, span_map: Sequence[NormalizationSegment]
) -> Iterator[dict[str, object]]:
    quote_patterns = (
        ("ascii_double", _DOUBLE_QUOTE_RE),
        ("curly_double", _CURLY_QUOTE_RE),
        ("guillemet", _GUILLEMET_RE),
    )
    for quote_kind, pattern in quote_patterns:
        for match in pattern.finditer(text):
            yield _indicator(
                text,
                match.start(),
                match.end(),
                span_map,
                kind=quote_kind,
            )


def _overlaps_any(start: int, end: int, spans: Sequence[tuple[int, int]]) -> bool:
    return any(
        other_end > start and other_start < end for other_start, other_end in spans
    )


def _sentences(text: str) -> list[str]:
    return [
        match.group(0).strip()
        for match in _SENTENCE_RE.finditer(text)
        if match.group(0).strip()
    ]


def _counts(
    original: str,
    normalized: str,
    words: Sequence[str],
    sentences: Sequence[str],
) -> dict[str, int | float]:
    paragraphs = [part for part in _PARAGRAPH_RE.split(normalized) if part.strip()]
    categories = Counter(unicodedata.category(character)[0] for character in normalized)
    return {
        "original_characters": len(original),
        "normalized_characters": len(normalized),
        "utf8_bytes": len(normalized.encode("utf-8")),
        "lines": normalized.count("\n") + 1 if normalized else 0,
        "paragraphs": len(paragraphs),
        "sentences_heuristic": len(sentences),
        "words": len(words),
        "tokens_heuristic": sum(1 for _ in _TOKEN_RE.finditer(normalized)),
        "letters": categories["L"],
        "numbers": categories["N"],
        "marks": categories["M"],
        "punctuation": categories["P"],
        "symbols": categories["S"],
        "separators": categories["Z"],
        "control_or_format": categories["C"],
        "whitespace": sum(character.isspace() for character in normalized),
        "emoji_candidates": sum(_is_emoji(character) for character in normalized),
    }


def _script_distribution(text: str) -> list[dict[str, object]]:
    scripts = Counter(
        _script_name(character) for character in text if not character.isspace()
    )
    total = sum(scripts.values())
    return [
        {
            "script": script,
            "characters": count,
            "fraction": _ratio(count, total),
        }
        for script, count in sorted(
            scripts.items(), key=lambda pair: (-pair[1], pair[0])
        )
    ]


def _script_name(character: str) -> str:
    category = unicodedata.category(character)
    if category.startswith(("P", "N", "S")):
        return "Common"
    if category.startswith("M"):
        return "Inherited"
    name = unicodedata.name(character, "")
    script_names = (
        "LATIN",
        "GREEK",
        "CYRILLIC",
        "ARABIC",
        "HEBREW",
        "DEVANAGARI",
        "BENGALI",
        "GURMUKHI",
        "GUJARATI",
        "ORIYA",
        "TAMIL",
        "TELUGU",
        "KANNADA",
        "MALAYALAM",
        "THAI",
        "LAO",
        "GEORGIAN",
        "ARMENIAN",
        "HANGUL",
        "HIRAGANA",
        "KATAKANA",
        "CJK",
        "IDEOGRAPH",
        "ETHIOPIC",
        "CHEROKEE",
        "CANADIAN SYLLABICS",
        "KHMER",
        "MYANMAR",
        "SINHALA",
        "TIBETAN",
    )
    for script in script_names:
        if script in name:
            if script in {"CJK", "IDEOGRAPH"}:
                return "Han"
            return script.title().replace("Canadian Syllabics", "Canadian_Aboriginal")
    return "Unknown"


def _language_hints(words: Sequence[str], text: str) -> list[dict[str, object]]:
    word_counts = Counter(words)
    hints: list[dict[str, object]] = []
    if len(words) >= 4:
        scored = []
        for language, stopwords in _LANGUAGE_STOPWORDS.items():
            distinct_hits = sum(word_counts[word] > 0 for word in stopwords)
            token_hits = sum(word_counts[word] for word in stopwords)
            if distinct_hits >= 2:
                scored.append((language, distinct_hits, token_hits))
        for language, distinct_hits, token_hits in sorted(
            scored, key=lambda value: (-value[2], -value[1], value[0])
        )[:3]:
            hints.append(
                {
                    "language": language,
                    "basis": "small_function_word_lexicon",
                    "confidence": "low",
                    "distinct_indicator_words": distinct_hits,
                    "indicator_tokens": token_hits,
                    "token_fraction": _ratio(token_hits, len(words)),
                }
            )

    letter_scripts = Counter(
        _script_name(character)
        for character in text
        if unicodedata.category(character).startswith("L")
    )
    if letter_scripts:
        script, count = letter_scripts.most_common(1)[0]
        hints.append(
            {
                "language": None,
                "basis": "dominant_script_only",
                "confidence": "not_a_language_identification",
                "script": script,
                "letter_fraction": _ratio(count, sum(letter_scripts.values())),
            }
        )
    return hints


def _repeated_content(
    text: str,
    sentences: Sequence[str],
    words: Sequence[str],
) -> dict[str, list[dict[str, object]]]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    normalized_sentences = [sentence.casefold().strip() for sentence in sentences]
    phrase_counts: Counter[str] = Counter()
    for size in (3, 4, 5):
        phrase_counts.update(
            " ".join(words[index : index + size])
            for index in range(max(0, len(words) - size + 1))
        )
    return {
        "lines": _repetition_values(Counter(lines)),
        "sentences": _repetition_values(Counter(normalized_sentences)),
        "word_sequences": _repetition_values(phrase_counts),
    }


def _repetition_values(counter: Counter[str]) -> list[dict[str, object]]:
    repeated = ((value, count) for value, count in counter.items() if count > 1)
    return [
        _bounded_text_value(value, count=count)
        for value, count in sorted(repeated, key=lambda pair: (-pair[1], pair[0]))[
            :MAX_REPETITION_VALUES
        ]
    ]


def _bounded_text_value(value: str, **extra: object) -> dict[str, object]:
    limit = 256
    return {
        "value": value[:limit],
        "value_truncated": len(value) > limit,
        "sha256": sha256(value.encode("utf-8")).hexdigest(),
        **extra,
    }


def _unusual_characters(
    text: str, span_map: Sequence[NormalizationSegment]
) -> list[dict[str, object]]:
    occurrences: dict[str, list[int]] = {}
    totals: Counter[str] = Counter()
    for index, character in enumerate(text):
        if not _is_unusual(character):
            continue
        totals[character] += 1
        positions = occurrences.setdefault(character, [])
        if len(positions) < MAX_POSITIONS_PER_UNUSUAL_CHARACTER:
            positions.append(index)

    values: list[dict[str, object]] = []
    for character, count in sorted(
        totals.items(), key=lambda pair: (-pair[1], ord(pair[0]))
    )[:MAX_UNUSUAL_CHARACTER_KINDS]:
        positions = occurrences[character]
        values.append(
            {
                "codepoint": f"U+{ord(character):04X}",
                "unicode_name": unicodedata.name(character, "UNNAMED"),
                "category": unicodedata.category(character),
                "count": count,
                "positions_truncated": count > len(positions),
                "spans": [
                    _source_span(position, position + 1, span_map)
                    for position in positions
                ],
            }
        )
    return values


def _is_unusual(character: str) -> bool:
    if character in {"\n", "\t"}:
        return False
    category = unicodedata.category(character)
    return category.startswith("C") or character in {
        "\u034f",
        "\u061c",
        "\u115f",
        "\u1160",
        "\u17b4",
        "\u17b5",
        "\u180e",
        "\u200b",
        "\u200c",
        "\u200d",
        "\u2060",
        "\ufeff",
    }


def _readability(words: Sequence[str], sentences: Sequence[str]) -> dict[str, object]:
    letter_words = [
        word for word in words if any(character.isalpha() for character in word)
    ]
    eligible_words = [
        word for word in letter_words if _has_latin_readability_form(word)
    ]
    lexical_coverage = _ratio(len(eligible_words), len(letter_words))
    syllables = sum(_estimate_syllables(word) for word in eligible_words)
    sentence_count = len(sentences)
    word_count = len(letter_words)
    if word_count == 0 or sentence_count == 0 or lexical_coverage < 0.8:
        return {
            "applicable": False,
            "basis": "English-oriented heuristic",
            "flesch_reading_ease": None,
            "flesch_kincaid_grade": None,
            "estimated_syllables": syllables,
            "latin_lexical_coverage": lexical_coverage,
        }
    words_per_sentence = word_count / sentence_count
    syllables_per_word = syllables / word_count
    return {
        "applicable": True,
        "basis": "English-oriented heuristic; not a language determination",
        "flesch_reading_ease": _round(
            206.835 - 1.015 * words_per_sentence - 84.6 * syllables_per_word
        ),
        "flesch_kincaid_grade": _round(
            0.39 * words_per_sentence + 11.8 * syllables_per_word - 15.59
        ),
        "estimated_syllables": syllables,
        "latin_lexical_coverage": lexical_coverage,
        "words_per_sentence": _round(words_per_sentence),
        "syllables_per_word": _round(syllables_per_word),
    }


def _estimate_syllables(word: str) -> int:
    ascii_word = "".join(
        character
        for character in unicodedata.normalize("NFKD", word.casefold())
        if "a" <= character <= "z"
    )
    if not ascii_word:
        return 1
    groups = len(re.findall(r"[aeiouy]+", ascii_word))
    if (
        ascii_word.endswith("e")
        and not ascii_word.endswith(("le", "ye"))
        and groups > 1
    ):
        groups -= 1
    return max(1, groups)


def _has_latin_readability_form(word: str) -> bool:
    return any(
        "a" <= character <= "z"
        for character in unicodedata.normalize("NFKD", word.casefold())
    )


def _lexical_profile(words: Sequence[str]) -> dict[str, object]:
    frequencies = Counter(words)
    word_count = len(words)
    unique = len(frequencies)
    return {
        "casefolded": True,
        "word_count": word_count,
        "unique_words": unique,
        "type_token_ratio": _ratio(unique, word_count),
        "root_type_token_ratio": _round(unique / math.sqrt(word_count))
        if word_count
        else 0.0,
        "hapax_words": sum(count == 1 for count in frequencies.values()),
        "dis_legomena_words": sum(count == 2 for count in frequencies.values()),
        "function_word_tokens": sum(
            count for word, count in frequencies.items() if word in _FUNCTION_WORDS
        ),
        "top_words": _counter_values(frequencies, MAX_NGRAM_VALUES),
    }


def _writing_style(
    text: str,
    words: Sequence[str],
    folded_words: Sequence[str],
    sentences: Sequence[str],
) -> dict[str, object]:
    word_lengths = [sum(character.isalnum() for character in word) for word in words]
    sentence_lengths = [len(_WORD_RE.findall(sentence)) for sentence in sentences]
    punctuation = Counter(
        character
        for character in text
        if unicodedata.category(character).startswith("P")
    )
    emoji = Counter(character for character in text if _is_emoji(character))
    contractions = Counter(
        word.casefold().replace("\u2019", "'")
        for word in words
        if "'" in word or "\u2019" in word
    )
    abbreviation_re = re.compile(
        r"(?i)(?<!\w)(?:[A-Z]\.){2,}|\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|vs|etc)\."
    )
    abbreviations = Counter(match.group(0) for match in abbreviation_re.finditer(text))
    word_counts = Counter(folded_words)
    function_words = Counter(
        {word: word_counts[word] for word in _FUNCTION_WORDS if word_counts[word]}
    )
    nonempty_lines = [line.strip() for line in text.splitlines() if line.strip()]
    greeting = (
        _line_marker(nonempty_lines[0], _GREETING_PATTERNS) if nonempty_lines else None
    )
    signoff = (
        _line_marker(nonempty_lines[-1], _SIGNOFF_PATTERNS) if nonempty_lines else None
    )

    alphabetic_words = [word for word in words if any(char.isalpha() for char in word)]
    uppercase_words = sum(word.isupper() and len(word) > 1 for word in alphabetic_words)
    titlecase_words = sum(word.istitle() for word in alphabetic_words)
    letters = [character for character in text if character.isalpha()]
    lines = text.splitlines()
    variant_values = []
    for american, british in _AMERICAN_BRITISH_PAIRS:
        if word_counts[american] or word_counts[british]:
            variant_values.append(
                {
                    "american_form": american,
                    "american_count": word_counts[american],
                    "british_form": british,
                    "british_count": word_counts[british],
                }
            )

    return {
        "distributions": {
            "word_length_characters": _distribution(word_lengths),
            "sentence_length_words_heuristic": _distribution(sentence_lengths),
        },
        "capitalization": {
            "uppercase_letters": sum(character.isupper() for character in letters),
            "lowercase_letters": sum(character.islower() for character in letters),
            "uppercase_letter_fraction": _ratio(
                sum(character.isupper() for character in letters), len(letters)
            ),
            "all_uppercase_words": uppercase_words,
            "titlecase_words": titlecase_words,
            "all_uppercase_word_fraction": _ratio(
                uppercase_words, len(alphabetic_words)
            ),
        },
        "spacing": {
            "spaces": text.count(" "),
            "tabs": text.count("\t"),
            "newlines": text.count("\n"),
            "multiple_space_runs": len(re.findall(r" {2,}", text)),
            "leading_space_lines": sum(
                bool(re.match(r"^[ \t]+\S", line)) for line in lines
            ),
            "trailing_space_lines": sum(
                bool(re.search(r"[ \t]+$", line)) for line in lines
            ),
        },
        "punctuation": _counter_values(punctuation, 100),
        "emoji_candidates": _counter_values(emoji, 100),
        "contractions": _counter_values(contractions, 100),
        "abbreviations": _counter_values(abbreviations, 100),
        "function_words": {
            "values": _counter_values(function_words, len(function_words)),
            "tokens_per_thousand_words": _round(
                1000 * sum(function_words.values()) / len(words)
            )
            if words
            else 0.0,
        },
        "greeting": greeting,
        "signoff": signoff,
        "spelling_variants": variant_values,
    }


def _line_marker(
    line: str, patterns: Sequence[re.Pattern[str]]
) -> dict[str, object] | None:
    for pattern in patterns:
        if match := pattern.search(line):
            return {
                "matched": match.group(0),
                "basis": (
                    "first_nonempty_line"
                    if patterns is _GREETING_PATTERNS
                    else "last_nonempty_line"
                ),
            }
    return None


def _distribution(values: Sequence[int]) -> dict[str, int | float | None]:
    if not values:
        return {
            "count": 0,
            "minimum": None,
            "maximum": None,
            "mean": None,
            "median": None,
        }
    return {
        "count": len(values),
        "minimum": min(values),
        "maximum": max(values),
        "mean": _round(mean(values)),
        "median": _round(median(values)),
    }


def _ngrams(
    text: str, words: Sequence[str]
) -> dict[str, dict[str, list[dict[str, object]]]]:
    folded_text = text.casefold()
    character_ngrams: dict[str, list[dict[str, object]]] = {}
    for size in (3, 4):
        values = Counter(
            folded_text[index : index + size]
            for index in range(max(0, len(folded_text) - size + 1))
            if not any(_is_unusual(char) for char in folded_text[index : index + size])
        )
        character_ngrams[str(size)] = _counter_values(values, MAX_NGRAM_VALUES)

    word_ngrams: dict[str, list[dict[str, object]]] = {}
    for size in (1, 2, 3):
        values = Counter(
            " ".join(words[index : index + size])
            for index in range(max(0, len(words) - size + 1))
        )
        word_ngrams[str(size)] = _counter_values(values, MAX_NGRAM_VALUES)
    return {"characters": character_ngrams, "words": word_ngrams}


def _counter_values(counter: Counter[str], limit: int) -> list[dict[str, object]]:
    values: list[dict[str, object]] = []
    for value, count in sorted(counter.items(), key=lambda pair: (-pair[1], pair[0]))[
        :limit
    ]:
        if len(value) <= 256:
            values.append({"value": value, "count": count})
        else:
            values.append(_bounded_text_value(value, count=count))
    return values


def _is_emoji(character: str) -> bool:
    code = ord(character)
    return (
        0x1F000 <= code <= 0x1FAFF
        or 0x2600 <= code <= 0x27BF
        or 0x2300 <= code <= 0x23FF
        or 0x2B00 <= code <= 0x2BFF
    )


def _ratio(numerator: int, denominator: int) -> float:
    return _round(numerator / denominator) if denominator else 0.0


def _round(value: float) -> float:
    return round(value, 6)
