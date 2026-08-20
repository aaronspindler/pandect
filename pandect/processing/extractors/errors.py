"""Sanitized failures shared by deterministic extractors."""


class ExtractorError(Exception):
    """A safe-to-display extraction failure.

    The exception intentionally retains neither command arguments nor captured
    output: both may contain sensitive evidence or local paths.
    """

    code = "extractor_error"
    public_message = "Local evidence extraction failed."

    def __init__(self, message: str | None = None):
        super().__init__(message or self.public_message)


class ToolUnavailableError(ExtractorError):
    code = "tool_unavailable"
    public_message = "A required local extraction tool is unavailable."


class OptionalDependencyUnavailableError(ExtractorError):
    code = "optional_dependency_unavailable"
    public_message = "An optional local extraction dependency is unavailable."


class ModelUnavailableError(ExtractorError):
    """A configured local inference model is not present on disk."""

    code = "local_model_unavailable"
    public_message = "A required local inference model is unavailable."


class ModelIntegrityError(ExtractorError):
    """A local inference model does not match its configured digest."""

    code = "local_model_integrity"
    public_message = "A local inference model failed its integrity check."


class ToolTimedOutError(ExtractorError):
    code = "tool_timed_out"
    public_message = "A local extraction tool exceeded its time limit."


class ToolOutputLimitError(ExtractorError):
    code = "tool_output_limit"
    public_message = "A local extraction tool exceeded its output limit."


class ToolFailedError(ExtractorError):
    code = "tool_failed"
    public_message = "A local extraction tool could not process this item."

    def __init__(self, *, returncode: int | None = None):
        self.returncode = returncode
        super().__init__()


class InvalidExtractorOutputError(ExtractorError):
    code = "invalid_tool_output"
    public_message = "A local extraction tool returned invalid output."


class ResourceLimitError(ExtractorError):
    code = "resource_limit"
    public_message = "The item exceeds a configured local processing limit."


class UnsupportedPixelFormatError(ExtractorError):
    """The image is valid evidence but this runtime cannot decode its pixels."""

    code = "unsupported_pixel_format"
    public_message = "The local runtime does not support this image pixel format."
