import json
import math

import pytest

from pandect.processing.extractors.errors import ResourceLimitError
from pandect.processing.extractors.text import (
    MAX_INDICATORS_PER_KIND,
    WRITING_IDENTITY_DIMENSION,
    WRITING_IDENTITY_FEATURE_DEFINITION_SHA256,
    WRITING_IDENTITY_FEATURE_NAMES,
    WRITING_IDENTITY_FEATURE_SPACE,
    build_writing_identity_vector,
    extract_message_text,
)


def indicator_values(result, kind):
    return result["indicators"][kind]["values"]


def test_normalization_preserves_a_stable_map_to_original_offsets():
    original = "Cafe\u0301\r\nEmail: person@example.test"

    result = extract_message_text(original)

    normalization = result["normalization"]
    assert normalization["text"] == "Caf\u00e9\nEmail: person@example.test"
    assert normalization["changed"] is True
    assert normalization["original_length"] == len(original)
    assert normalization["normalized_length"] == len(normalization["text"])
    assert {segment["kind"] for segment in normalization["span_map"]} == {
        "identity",
        "newline",
        "unicode_nfc",
    }

    email = indicator_values(result, "email_addresses")[0]
    normalized_start = normalization["text"].index("person@example.test")
    original_start = original.index("person@example.test")
    assert email["span"] == {
        "normalized_start": normalized_start,
        "normalized_end": normalized_start + len("person@example.test"),
        "original_start": original_start,
        "original_end": original_start + len("person@example.test"),
    }


def test_indicator_extraction_returns_validated_candidates_and_provenance_spans():
    digest = "a" * 64
    message = (
        "Write person@example.test or visit https://example.test/path?q=1. "
        "Host 203.0.113.9 and [2001:db8::2]; call +1 (416) 555-0199. "
        "Find @operator #Evidence at 43.6532,-79.3832. "
        f"Digest {digest}; MAC 00:11:22:33:44:55; ID CASE-1042. "
        "Budget CAD 1,250.00 on 2026-08-12 at 14:30Z. "
        "\u201cRetain this quoted passage.\u201d"
    )

    result = extract_message_text(message)

    assert indicator_values(result, "email_addresses")[0]["value"] == (
        "person@example.test"
    )
    assert {value["kind"] for value in indicator_values(result, "domains")} == {
        "domain_candidate",
        "email_domain",
    }
    assert indicator_values(result, "urls")[0]["value"] == (
        "https://example.test/path?q=1"
    )
    assert indicator_values(result, "ipv4_addresses")[0]["normalized_value"] == (
        "203.0.113.9"
    )
    assert indicator_values(result, "ipv6_addresses")[0]["normalized_value"] == (
        "2001:db8::2"
    )
    assert indicator_values(result, "phone_candidates")[0]["normalized_value"] == (
        "+14165550199"
    )
    assert indicator_values(result, "handles")[0]["value"] == "@operator"
    assert indicator_values(result, "hashtags")[0]["value"] == "#Evidence"
    coordinate = indicator_values(result, "coordinate_candidates")[0]
    assert coordinate["attributes"]["latitude"] == 43.6532
    assert coordinate["attributes"]["longitude"] == -79.3832
    assert indicator_values(result, "cryptographic_hashes")[0]["kind"] == (
        "sha256_candidate"
    )
    assert indicator_values(result, "mac_addresses")[0]["normalized_value"] == (
        "00:11:22:33:44:55"
    )
    assert indicator_values(result, "identifiers")[0]["kind"] == ("labelled_identifier")
    assert indicator_values(result, "currency_candidates")[0]["value"] == (
        "CAD 1,250.00"
    )
    assert indicator_values(result, "date_candidates")[0]["attributes"] == {
        "format_hint": "year_first",
        "candidate_only": True,
    }
    assert indicator_values(result, "time_candidates")[0]["value"] == "14:30Z"
    assert indicator_values(result, "quoted_passages")[0]["kind"] == "curly_double"
    assert all(
        "span" in values[0]
        for collection in result["indicators"].values()
        if (values := collection["values"])
    )


def test_writing_profile_is_deterministic_bounded_and_json_compatible():
    message = (
        "Hello team,\n\n"
        "The colour is BLUE, and the colour is bright!  I can't change it. \U0001f642\n"
        "The colour is BLUE, and the colour is bright!\n\n"
        "Kind regards"
    )

    first = extract_message_text(message)
    second = extract_message_text(message)

    assert first == second
    json.dumps(first, allow_nan=False)
    assert first["schema_version"] == 1
    assert first["counts"]["paragraphs"] == 3
    assert first["counts"]["words"] > 10
    assert first["counts"]["emoji_candidates"] == 1
    assert first["lexical_profile"]["unique_words"] < first["counts"]["words"]
    assert first["readability"]["applicable"] is True
    assert first["writing_style"]["capitalization"]["all_uppercase_words"] >= 2
    assert first["writing_style"]["spacing"]["multiple_space_runs"] == 1
    assert first["writing_style"]["greeting"]["matched"] == "Hello"
    assert first["writing_style"]["signoff"]["matched"] == "Kind regards"
    assert first["writing_style"]["spelling_variants"] == [
        {
            "american_form": "color",
            "american_count": 0,
            "british_form": "colour",
            "british_count": 4,
        }
    ]
    assert any(
        value["count"] >= 2 for value in first["repeated_content"]["word_sequences"]
    )
    assert len(first["ngrams"]["characters"]["3"]) <= 100
    assert len(first["ngrams"]["words"]["3"]) <= 100
    assert any(hint["language"] == "en" for hint in first["language_hints"])


def test_script_and_unusual_character_results_are_descriptive_not_literal():
    result = extract_message_text(
        "Latin \u041a\u0438\u0440\u0438\u043b\u043b\u0438\u0446\u0430\u200btext"
    )

    scripts = {entry["script"] for entry in result["scripts"]}
    assert {"Latin", "Cyrillic"} <= scripts
    assert result["unusual_characters"] == [
        {
            "codepoint": "U+200B",
            "unicode_name": "ZERO WIDTH SPACE",
            "category": "Cf",
            "count": 1,
            "positions_truncated": False,
            "spans": [
                {
                    "normalized_start": 15,
                    "normalized_end": 16,
                    "original_start": 15,
                    "original_end": 16,
                }
            ],
        }
    ]
    assert "\u200b" not in json.dumps(result["unusual_characters"])


def test_empty_text_has_complete_zero_safe_output():
    result = extract_message_text("")

    assert result["normalization"]["span_map"] == []
    assert result["counts"]["words"] == 0
    assert result["counts"]["lines"] == 0
    assert result["scripts"] == []
    assert result["readability"]["applicable"] is False
    assert result["lexical_profile"]["type_token_ratio"] == 0.0
    assert all(collection["total"] == 0 for collection in result["indicators"].values())


def test_non_latin_text_does_not_receive_english_readability_scores():
    result = extract_message_text("Привет мир. Как дела?")

    assert result["readability"]["applicable"] is False
    assert result["readability"]["flesch_reading_ease"] is None
    assert result["readability"]["latin_lexical_coverage"] == 0.0


def test_very_long_feature_values_are_hashed_and_truncated():
    result = extract_message_text("x" * 1_000)

    top_word = result["lexical_profile"]["top_words"][0]
    assert len(top_word["value"]) == 256
    assert top_word["value_truncated"] is True
    assert len(top_word["sha256"]) == 64


def test_resource_and_indicator_limits_are_explicit_and_sanitized():
    with pytest.raises(ResourceLimitError) as error:
        extract_message_text("private evidence", max_characters=5)

    assert str(error.value) == "The item exceeds a configured local processing limit."
    assert "private evidence" not in str(error.value)

    emails = " ".join(
        f"person{index}@example.test" for index in range(MAX_INDICATORS_PER_KIND + 5)
    )
    result = extract_message_text(emails)
    collection = result["indicators"]["email_addresses"]
    assert collection["total"] == MAX_INDICATORS_PER_KIND + 5
    assert collection["returned"] == MAX_INDICATORS_PER_KIND
    assert collection["truncated"] is True


def test_invalid_inputs_fail_without_echoing_values():
    with pytest.raises(TypeError, match="text must be a string"):
        extract_message_text(b"secret")
    with pytest.raises(ValueError, match="max_characters must be positive"):
        extract_message_text("text", max_characters=0)


def test_writing_identity_vector_has_a_stable_normalized_feature_space():
    profile = extract_message_text(
        "Hello there! I can't attend today, but I will write again. Kind regards"
    )

    first = build_writing_identity_vector(profile)
    second = build_writing_identity_vector(profile)

    assert first == second
    assert first["feature_space"] == WRITING_IDENTITY_FEATURE_SPACE
    assert first["feature_space_version"] == 1
    assert first["dimension"] == WRITING_IDENTITY_DIMENSION
    assert first["feature_names"] == list(WRITING_IDENTITY_FEATURE_NAMES)
    assert len(first["vector"]) == WRITING_IDENTITY_DIMENSION
    assert all(math.isfinite(value) for value in first["vector"])
    assert math.sqrt(sum(value * value for value in first["vector"])) == (
        pytest.approx(1.0, abs=1e-10)
    )
    assert first["word_count"] == profile["counts"]["words"]
    assert first["has_signal"] is True
    assert first["provenance"]["feature_definition_sha256"] == (
        WRITING_IDENTITY_FEATURE_DEFINITION_SHA256
    )
    assert first["provenance"]["source_text_included"] is False


def test_writing_identity_contract_never_returns_observed_source_text():
    secret = "ZebracornClassifiedToken"
    profile = extract_message_text(
        f"{secret} appears repeatedly. {secret} appears repeatedly."
    )

    vector = build_writing_identity_vector(profile)
    serialized = json.dumps(vector, sort_keys=True)

    assert secret not in serialized
    assert secret.casefold() not in serialized
    assert profile["normalization"]["text"] not in serialized
    assert all(secret.casefold() not in name for name in vector["feature_names"])


def test_writing_vector_ignores_normalized_text_and_uses_bounded_profile_only():
    profile = extract_message_text("A short, consistent writing sample.")
    before = build_writing_identity_vector(profile)
    profile["normalization"]["text"] = "replacement source text"

    after = build_writing_identity_vector(profile)

    assert after == before


def test_writing_identity_dimension_is_fixed_and_empty_input_is_finite():
    empty = build_writing_identity_vector(extract_message_text(""))
    punctuation = build_writing_identity_vector(extract_message_text("!!! -- ???"))

    assert empty["feature_names"] == punctuation["feature_names"]
    assert empty["dimension"] == punctuation["dimension"] == WRITING_IDENTITY_DIMENSION
    assert empty["vector"] == [0.0] * WRITING_IDENTITY_DIMENSION
    assert empty["word_count"] == 0
    assert empty["has_signal"] is False
    assert all(math.isfinite(value) for value in punctuation["vector"])
    assert punctuation["has_signal"] is False


def test_writing_identity_hashed_ngrams_distinguish_content_without_exposing_it():
    first = build_writing_identity_vector(
        extract_message_text("azure lantern circles softly")
    )
    second = build_writing_identity_vector(
        extract_message_text("bronze meadow drifts slowly")
    )

    assert first["dimension"] == second["dimension"]
    assert first["vector"] != second["vector"]


def test_writing_identity_feature_definition_digest_is_version_locked():
    assert WRITING_IDENTITY_DIMENSION == 331
    assert WRITING_IDENTITY_FEATURE_DEFINITION_SHA256 == (
        "661e9b7a50f5bcef43ae63338ce94ab9fa21c9e600862ab477ff0983fa79c0f0"
    )


def test_writing_identity_rejects_unknown_schema_without_echoing_profile_data():
    profile = extract_message_text("private writing evidence")
    profile["schema_version"] = 999

    with pytest.raises(ValueError) as error:
        build_writing_identity_vector(profile)

    assert str(error.value) == "unsupported text extraction schema"
    assert "private writing evidence" not in str(error.value)
