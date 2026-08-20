"""Deterministic local extractors used by processing stages.

The modules in this package deliberately do not know about Django models or Huey.
They accept paths/values and return immutable, JSON-compatible facts so the task
layer can decide how and when to persist them.
"""

from pandect.processing.extractors.errors import (
    ExtractorError,
    InvalidExtractorOutputError,
    ModelIntegrityError,
    ModelUnavailableError,
    OptionalDependencyUnavailableError,
    ResourceLimitError,
    ToolFailedError,
    ToolOutputLimitError,
    ToolTimedOutError,
    ToolUnavailableError,
)
from pandect.processing.extractors.hashing import (
    FileFingerprint,
    fingerprint_file,
    fingerprint_stream,
    sha256_bytes,
)

__all__ = [
    "ExtractorError",
    "FileFingerprint",
    "InvalidExtractorOutputError",
    "ModelIntegrityError",
    "ModelUnavailableError",
    "OptionalDependencyUnavailableError",
    "ResourceLimitError",
    "ToolFailedError",
    "ToolOutputLimitError",
    "ToolTimedOutError",
    "ToolUnavailableError",
    "fingerprint_file",
    "fingerprint_stream",
    "sha256_bytes",
]
