"""Bounded failure locations without exception text, source lines or locals."""

import logging
from collections import deque
from pathlib import Path
from traceback import walk_tb

from django.conf import settings
from django.core.exceptions import ValidationError

from core.analysis.errors import ERROR_MESSAGES
from core.analysis.operations import OPERATIONS

logger = logging.getLogger("core.analysis.failures")

MODEL_VALIDATION_CODES = frozenset(
    {
        "model_result_structure",
        "model_suggestion_citations",
        "model_citation_structure",
        "model_citation_source",
        "model_quote_offset_required",
        "model_quote_mismatch",
    }
)


def log_analysis_failure(run, error, code, stage):
    """Log only selected primitive metadata; never attach the exception object."""
    locations = deque(maxlen=8)
    for frame, line in walk_tb(error.__traceback__):
        try:
            path = Path(frame.f_code.co_filename).relative_to(settings.BASE_DIR)
        except ValueError:
            continue
        if path.suffix != ".py":
            continue
        parts = (*path.parts[:-1], path.stem)
        if not all(part.isidentifier() for part in parts):
            continue
        function = frame.f_code.co_name
        if not function.isidentifier():
            function = "anonymous"
        locations.append(f"{'.'.join(parts)[:200]}:{function[:80]}:{line}")
    validation_code = getattr(error, "code", None)
    if (
        not isinstance(error, ValidationError)
        or not isinstance(validation_code, str)
        or validation_code not in MODEL_VALIDATION_CODES
    ):
        validation_code = "none"
    exception_type = type(error).__name__
    if not exception_type.isidentifier():
        exception_type = "Exception"
    try:
        logger.error(
            "Analysis failed run_id=%s operation=%s error_code=%s stage=%s "
            "exception_type=%s validation_code=%s locations=%s",
            str(run.pk),
            run.operation if run.operation in OPERATIONS else "unknown",
            code if code in ERROR_MESSAGES else "processing_failed",
            stage,
            exception_type[:80],
            validation_code,
            ",".join(locations) or "unavailable",
        )
    except Exception:
        # A broken log sink must not prevent retained failure state or cleanup.
        return
