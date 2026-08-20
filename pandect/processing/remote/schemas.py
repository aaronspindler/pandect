"""Strict response schemas for consented model-generated hypotheses."""

SEMANTIC_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "summary",
        "language",
        "entities",
        "topics",
        "claims",
        "questions",
        "requests",
        "commitments",
        "events",
        "dates",
        "locations",
        "sentiment",
        "tone",
        "intent_hypotheses",
        "style_observations",
        "contradictions",
        "uncertainties",
        "language_cues",
        "summary_evidence",
        "language_evidence",
    ],
    "properties": {
        "summary": {"type": "string", "minLength": 1},
        "language": {"type": "string", "minLength": 1},
        "summary_evidence": {
            "type": "array",
            "minItems": 1,
            "items": {"$ref": "#/$defs/evidence"},
        },
        "language_evidence": {
            "type": "array",
            "minItems": 1,
            "items": {"$ref": "#/$defs/evidence"},
        },
        "entities": {"type": "array", "items": {"$ref": "#/$defs/evidence"}},
        "topics": {"type": "array", "items": {"$ref": "#/$defs/evidence"}},
        "claims": {"type": "array", "items": {"$ref": "#/$defs/evidence"}},
        "questions": {"type": "array", "items": {"$ref": "#/$defs/evidence"}},
        "requests": {"type": "array", "items": {"$ref": "#/$defs/evidence"}},
        "commitments": {"type": "array", "items": {"$ref": "#/$defs/evidence"}},
        "events": {"type": "array", "items": {"$ref": "#/$defs/evidence"}},
        "dates": {"type": "array", "items": {"$ref": "#/$defs/evidence"}},
        "locations": {"type": "array", "items": {"$ref": "#/$defs/evidence"}},
        "sentiment": {"type": "array", "items": {"$ref": "#/$defs/evidence"}},
        "tone": {"type": "array", "items": {"$ref": "#/$defs/evidence"}},
        "intent_hypotheses": {
            "type": "array",
            "items": {"$ref": "#/$defs/evidence"},
        },
        "style_observations": {
            "type": "array",
            "items": {"$ref": "#/$defs/evidence"},
        },
        "contradictions": {
            "type": "array",
            "items": {"$ref": "#/$defs/evidence"},
        },
        "uncertainties": {
            "type": "array",
            "items": {"$ref": "#/$defs/evidence"},
        },
        "language_cues": {
            "type": "array",
            "items": {"$ref": "#/$defs/evidence"},
        },
    },
    "$defs": {
        "evidence": {
            "type": "object",
            "additionalProperties": False,
            "required": ["text", "start", "end", "confidence"],
            "properties": {
                "text": {"type": "string", "minLength": 1},
                "start": {"type": "integer", "minimum": 0},
                "end": {"type": "integer", "minimum": 0},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
        }
    },
}


TRANSCRIPT_ANALYSIS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "summary",
        "language",
        "speaker_turn_summaries",
        "conversation_structure",
        "speech_style_observations",
        "topics",
        "entities",
        "claims",
        "questions",
        "commitments",
        "important_timestamps",
        "uncertainties",
    ],
    "properties": {
        "summary": {"$ref": "#/$defs/evidence"},
        "language": {"type": "string", "minLength": 1},
        "speaker_turn_summaries": {
            "type": "array",
            "items": {"$ref": "#/$defs/speakerTurn"},
        },
        "conversation_structure": {
            "type": "array",
            "items": {"$ref": "#/$defs/evidence"},
        },
        "speech_style_observations": {
            "type": "array",
            "items": {"$ref": "#/$defs/evidence"},
        },
        "topics": {"type": "array", "items": {"$ref": "#/$defs/evidence"}},
        "entities": {"type": "array", "items": {"$ref": "#/$defs/evidence"}},
        "claims": {"type": "array", "items": {"$ref": "#/$defs/evidence"}},
        "questions": {
            "type": "array",
            "items": {"$ref": "#/$defs/evidence"},
        },
        "commitments": {
            "type": "array",
            "items": {"$ref": "#/$defs/evidence"},
        },
        "important_timestamps": {
            "type": "array",
            "items": {"$ref": "#/$defs/timedEvidence"},
        },
        "uncertainties": {
            "type": "array",
            "items": {"$ref": "#/$defs/evidence"},
        },
    },
    "$defs": {
        "evidence": {
            "type": "object",
            "additionalProperties": False,
            "required": ["text", "start", "end", "confidence"],
            "properties": {
                "text": {"type": "string"},
                "start": {"type": "integer", "minimum": 0},
                "end": {"type": "integer", "minimum": 0},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
        },
        "speakerTurn": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "text",
                "start",
                "end",
                "confidence",
                "speaker_label",
                "stream_index",
                "start_seconds",
                "end_seconds",
            ],
            "properties": {
                "text": {"type": "string"},
                "start": {"type": "integer", "minimum": 0},
                "end": {"type": "integer", "minimum": 0},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "speaker_label": {"type": "string", "minLength": 1},
                "stream_index": {"type": ["integer", "null"], "minimum": 0},
                "start_seconds": {"type": "number", "minimum": 0},
                "end_seconds": {"type": "number", "minimum": 0},
            },
        },
        "timedEvidence": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "text",
                "start",
                "end",
                "confidence",
                "stream_index",
                "start_seconds",
                "end_seconds",
            ],
            "properties": {
                "text": {"type": "string"},
                "start": {"type": "integer", "minimum": 0},
                "end": {"type": "integer", "minimum": 0},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "stream_index": {"type": ["integer", "null"], "minimum": 0},
                "start_seconds": {"type": "number", "minimum": 0},
                "end_seconds": {"type": "number", "minimum": 0},
            },
        },
    },
}

VISION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "description",
        "objects",
        "actions",
        "people_count",
        "visible_appearance",
        "logos",
        "landmarks",
        "visible_text",
        "setting",
        "time_weather_hypotheses",
        "anomalies",
        "uncertainties",
    ],
    "properties": {
        "description": {"type": "string"},
        "objects": {"type": "array", "items": {"type": "string"}},
        "actions": {"type": "array", "items": {"type": "string"}},
        "people_count": {"type": ["integer", "null"], "minimum": 0},
        "visible_appearance": {"type": "array", "items": {"type": "string"}},
        "logos": {"type": "array", "items": {"type": "string"}},
        "landmarks": {"type": "array", "items": {"type": "string"}},
        "visible_text": {"type": "array", "items": {"type": "string"}},
        "setting": {"type": "string"},
        "time_weather_hypotheses": {
            "type": "array",
            "items": {"type": "string"},
        },
        "anomalies": {"type": "array", "items": {"type": "string"}},
        "uncertainties": {"type": "array", "items": {"type": "string"}},
        "grounding": {
            "type": "array",
            "items": {"$ref": "#/$defs/grounding"},
        },
    },
    "$defs": {
        "grounding": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "field",
                "observation_index",
                "text",
                "confidence",
                "region",
            ],
            "properties": {
                "field": {
                    "enum": [
                        "description",
                        "objects",
                        "actions",
                        "visible_appearance",
                        "logos",
                        "landmarks",
                        "visible_text",
                        "setting",
                        "time_weather_hypotheses",
                        "anomalies",
                        "uncertainties",
                    ]
                },
                "observation_index": {
                    "type": ["integer", "null"],
                    "minimum": 0,
                },
                "text": {"type": "string", "minLength": 1},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "region": {"$ref": "#/$defs/normalizedRegion"},
            },
        },
        "normalizedRegion": {
            "type": "object",
            "additionalProperties": False,
            "$comment": ("Coordinates are normalized to the original image bounds."),
            "required": ["x_min", "y_min", "x_max", "y_max"],
            "properties": {
                "x_min": {"type": "number", "minimum": 0, "maximum": 1},
                "y_min": {"type": "number", "minimum": 0, "maximum": 1},
                "x_max": {"type": "number", "minimum": 0, "maximum": 1},
                "y_max": {"type": "number", "minimum": 0, "maximum": 1},
            },
        },
    },
}


VIDEO_ACCOUNT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "summary",
        "scene_timeline",
        "people",
        "objects",
        "actions",
        "dialogue_summary",
        "important_events",
        "claims",
        "uncertainties",
    ],
    "properties": {
        "summary": {"$ref": "#/$defs/evidence"},
        "scene_timeline": {
            "type": "array",
            "items": {"$ref": "#/$defs/timedEvidence"},
        },
        "people": {"type": "array", "items": {"$ref": "#/$defs/evidence"}},
        "objects": {"type": "array", "items": {"$ref": "#/$defs/evidence"}},
        "actions": {"type": "array", "items": {"$ref": "#/$defs/evidence"}},
        "dialogue_summary": {
            "type": "array",
            "items": {"$ref": "#/$defs/evidence"},
        },
        "important_events": {
            "type": "array",
            "items": {"$ref": "#/$defs/evidence"},
        },
        "claims": {"type": "array", "items": {"$ref": "#/$defs/evidence"}},
        "uncertainties": {
            "type": "array",
            "items": {"$ref": "#/$defs/evidence"},
        },
    },
    "$defs": {
        "evidence": {
            "type": "object",
            "additionalProperties": False,
            "required": ["text", "source_entry_indexes", "confidence"],
            "properties": {
                "text": {"type": "string"},
                "source_entry_indexes": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "integer", "minimum": 0},
                    "uniqueItems": True,
                },
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
        },
        "timedEvidence": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "text",
                "start_seconds",
                "end_seconds",
                "source_entry_indexes",
                "confidence",
            ],
            "properties": {
                "text": {"type": "string"},
                "start_seconds": {"type": ["number", "null"], "minimum": 0},
                "end_seconds": {"type": ["number", "null"], "minimum": 0},
                "source_entry_indexes": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "integer", "minimum": 0},
                    "uniqueItems": True,
                },
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
        },
    },
}

TRANSCRIPTION_SCHEMA = {
    "type": "object",
    "required": ["channels", "utterances"],
    "allOf": [
        {
            "if": {
                "required": ["channels"],
                "properties": {
                    "channels": {
                        "contains": {
                            "required": ["alternatives"],
                            "properties": {
                                "alternatives": {
                                    "contains": {
                                        "required": ["transcript"],
                                        "properties": {
                                            "transcript": {
                                                "type": "string",
                                                "pattern": r"\S",
                                            }
                                        },
                                    }
                                }
                            },
                        }
                    }
                },
            },
            "then": {"properties": {"utterances": {"minItems": 1}}},
            "else": {"properties": {"utterances": {"maxItems": 0}}},
        }
    ],
    "properties": {
        "channels": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": [
                    "alternatives",
                    "detected_language",
                    "language_confidence",
                ],
                "properties": {
                    "detected_language": {"type": "string", "minLength": 1},
                    "language_confidence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                    },
                    "alternatives": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "required": ["transcript", "confidence", "words"],
                            "allOf": [
                                {
                                    "if": {
                                        "required": ["transcript"],
                                        "properties": {
                                            "transcript": {
                                                "type": "string",
                                                "pattern": r"\S",
                                            }
                                        },
                                    },
                                    "then": {"properties": {"words": {"minItems": 1}}},
                                    "else": {"properties": {"words": {"maxItems": 0}}},
                                }
                            ],
                            "properties": {
                                "transcript": {"type": "string"},
                                "confidence": {
                                    "type": "number",
                                    "minimum": 0,
                                    "maximum": 1,
                                },
                                "words": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "required": [
                                            "word",
                                            "start",
                                            "end",
                                            "confidence",
                                            "speaker",
                                        ],
                                        "properties": {
                                            "word": {
                                                "type": "string",
                                                "minLength": 1,
                                            },
                                            "punctuated_word": {"type": "string"},
                                            "start": {"type": "number", "minimum": 0},
                                            "end": {"type": "number", "minimum": 0},
                                            "confidence": {
                                                "type": "number",
                                                "minimum": 0,
                                                "maximum": 1,
                                            },
                                            "speaker": {
                                                "type": "integer",
                                                "minimum": 0,
                                            },
                                            "speaker_confidence": {
                                                "type": "number",
                                                "minimum": 0,
                                                "maximum": 1,
                                            },
                                        },
                                        "additionalProperties": True,
                                    },
                                },
                            },
                            "additionalProperties": True,
                        },
                    },
                },
                "additionalProperties": True,
            },
        },
        "utterances": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "start",
                    "end",
                    "confidence",
                    "channel",
                    "transcript",
                    "words",
                    "speaker",
                ],
                "properties": {
                    "start": {"type": "number", "minimum": 0},
                    "end": {"type": "number", "minimum": 0},
                    "transcript": {"type": "string", "pattern": r"\S"},
                    "confidence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                    },
                    "channel": {"type": "integer", "minimum": 0},
                    "speaker": {"type": "integer", "minimum": 0},
                    "words": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "required": [
                                "word",
                                "start",
                                "end",
                                "confidence",
                                "speaker",
                            ],
                            "properties": {
                                "word": {"type": "string", "minLength": 1},
                                "punctuated_word": {"type": "string"},
                                "start": {"type": "number", "minimum": 0},
                                "end": {"type": "number", "minimum": 0},
                                "confidence": {
                                    "type": "number",
                                    "minimum": 0,
                                    "maximum": 1,
                                },
                                "speaker": {"type": "integer", "minimum": 0},
                                "speaker_confidence": {
                                    "type": "number",
                                    "minimum": 0,
                                    "maximum": 1,
                                },
                            },
                            "additionalProperties": True,
                        },
                    },
                },
                "additionalProperties": True,
            },
        },
    },
    "additionalProperties": True,
}


def validate_source_spans(value: dict, source: str) -> list[dict]:
    """Return warnings without discarding otherwise valid model output."""
    warnings = []
    for key, entries in value.items():
        if isinstance(entries, dict) and {"start", "end"} <= entries.keys():
            records = [entries]
        elif isinstance(entries, list):
            records = entries
        else:
            continue
        for index, entry in enumerate(records):
            if (
                not isinstance(entry, dict)
                or "start" not in entry
                or "end" not in entry
            ):
                continue
            start, end = entry["start"], entry["end"]
            if not isinstance(start, int) or not isinstance(end, int):
                warnings.append({"field": key, "index": index, "code": "invalid_span"})
            elif start < 0 or end < start or end > len(source):
                warnings.append({"field": key, "index": index, "code": "invalid_span"})
            elif entry.get("text") != source[start:end]:
                warnings.append(
                    {"field": key, "index": index, "code": "span_text_mismatch"}
                )
    return warnings


def validate_source_spans_strict(value: dict, source: str) -> None:
    """Reject invalid transcript citations for separately consented analysis."""
    if validate_source_spans(value, source):
        raise ValueError("The model response cited an invalid source span.")
