"""Typed adapters for the fixed media operations; never a local fallback.

The child context is entered only by the confined executable. Unit tests may
explicitly enter it to exercise deterministic algorithms without a daemon.
There is deliberately no environment setting that disables isolation.
"""

import inspect
import math
import types
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import fields, is_dataclass, replace
from functools import wraps
from pathlib import Path
from typing import Any, Union, get_args, get_origin, get_type_hints

from pandect.processing.extractors.errors import InvalidExtractorOutputError

_IN_CHILD = ContextVar("pandect_confined_media_child", default=False)
EXECUTION_POLICY_VERSION = "isolated-media-runner-v1"


@contextmanager
def child_execution():
    token = _IN_CHILD.set(True)
    try:
        yield
    finally:
        _IN_CHILD.reset(token)


def require_child() -> None:
    if not _IN_CHILD.get():
        raise RuntimeError("Native media execution requires the confined runner.")


def decode_result(annotation, value):
    """Validate against code-owned result types, never payload-selected types."""
    origin = get_origin(annotation)
    args = get_args(annotation)
    if annotation is Any:
        if value is None or type(value) in (str, bool, int):
            return value
        if type(value) is float and math.isfinite(value):
            return value
        if isinstance(value, list):
            return [decode_result(Any, item) for item in value]
        if isinstance(value, dict) and all(type(key) is str for key in value):
            return {key: decode_result(Any, item) for key, item in value.items()}
        raise InvalidExtractorOutputError
    if origin in (Union, types.UnionType):
        for candidate in args:
            try:
                return decode_result(candidate, value)
            except InvalidExtractorOutputError:
                pass
        raise InvalidExtractorOutputError
    if annotation is type(None):
        if value is not None:
            raise InvalidExtractorOutputError
        return None
    if is_dataclass(annotation):
        names = {field.name for field in fields(annotation)}
        if not isinstance(value, dict) or set(value) != names:
            raise InvalidExtractorOutputError
        hints = get_type_hints(annotation)
        return annotation(
            **{name: decode_result(hints[name], item) for name, item in value.items()}
        )
    if origin in (dict, Mapping):
        if not isinstance(value, dict):
            raise InvalidExtractorOutputError
        key_type, item_type = args or (str, Any)
        return {
            decode_result(key_type, key): decode_result(item_type, item)
            for key, item in value.items()
        }
    if origin in (list, tuple, Sequence):
        if not isinstance(value, list):
            raise InvalidExtractorOutputError
        if origin is tuple and args and args[-1] is not Ellipsis:
            if len(value) != len(args):
                raise InvalidExtractorOutputError
            items = [
                decode_result(kind, item)
                for kind, item in zip(args, value, strict=True)
            ]
        else:
            items = [decode_result(args[0] if args else Any, item) for item in value]
        return tuple(items) if origin is tuple else items
    if annotation is float:
        try:
            valid = type(value) in (int, float) and math.isfinite(value)
        except OverflowError:
            valid = False
        if not valid:
            raise InvalidExtractorOutputError
        return float(value)
    if annotation in (str, bool, int):
        if type(value) is not annotation:
            raise InvalidExtractorOutputError
        return value
    raise InvalidExtractorOutputError


def operation_timeout(operation: str, parameters: dict) -> float:
    if "timeout_seconds" in parameters:
        multiplier = (
            2
            if operation == "extract_ocr" and parameters.get("include_orientation")
            else 1
        )
        return min(7200, max(1, float(parameters["timeout_seconds"]) * multiplier))
    if operation in {"analyze_image", "render_image_frame", "analyze_faces"}:
        return 120
    from pandect.processing.extractors.audio import ffmpeg_timeout_seconds

    duration = parameters.get("duration_seconds")
    if duration is None and "end_seconds" in parameters:
        duration = parameters["end_seconds"] - parameters.get("start_seconds", 0)
    timeout = ffmpeg_timeout_seconds(duration)
    return min(7200, timeout * (2 if operation == "analyze_acoustics" else 1))


def runner_result(operation: str, **kwargs) -> dict:
    from pandect.media_runner.client import RunnerJobError, run_operation
    from pandect.processing.extractors import errors

    try:
        return run_operation(operation, **kwargs)
    except RunnerJobError as error:
        known = (
            errors.ToolUnavailableError,
            errors.OptionalDependencyUnavailableError,
            errors.ModelUnavailableError,
            errors.ModelIntegrityError,
            errors.ToolTimedOutError,
            errors.ToolOutputLimitError,
            errors.ToolFailedError,
            errors.InvalidExtractorOutputError,
            errors.ResourceLimitError,
            errors.UnsupportedPixelFormatError,
        )
        for kind in known:
            if error.code == kind.code:
                raise kind() from error
        raise errors.ToolFailedError() from error


def isolated(operation: str, *, source: str, destination: str | None = None):
    """Route one concrete extractor while retaining its typed Python API."""

    def decorate(function):
        signature = inspect.signature(function)

        @wraps(function)
        def call(*args, **kwargs):
            if _IN_CHILD.get():
                return function(*args, **kwargs)
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            parameters = dict(bound.arguments)
            if operation == "extract_subtitle_stream":
                from pandect.processing.extractors.derived_media import (
                    subtitle_output_for_codec,
                )

                if subtitle_output_for_codec(parameters["codec_name"]) is None:
                    return None
            source_path = Path(parameters.pop(source))
            destination_path = (
                Path(parameters.pop(destination)) if destination is not None else None
            )
            if destination_path is not None:
                if destination_path.exists():
                    raise FileExistsError("The derived destination already exists.")
                if source_path.resolve() == destination_path.resolve():
                    raise ValueError("A derivative cannot overwrite its source.")
                parameters["output_name"] = destination_path.name
            progress_callback = parameters.pop("progress_callback", None)
            for name in ("runner", "runtime"):
                value = parameters.pop(name, None)
                if name != "progress_callback" and value not in (
                    None,
                    signature.parameters.get(
                        name,
                        inspect.Parameter("unused", inspect.Parameter.KEYWORD_ONLY),
                    ).default,
                ):
                    raise ValueError("Execution overrides are not runner parameters.")
            for name in (
                "ffmpeg_binary",
                "ffprobe_binary",
                "exiftool_binary",
                "tesseract_binary",
            ):
                value = parameters.pop(name, None)
                if value is not None and value != signature.parameters[name].default:
                    raise ValueError("Only the fixed extraction tools are supported.")
            # Models are the fixed, digest-bound public manifest mounted in the
            # runner. Never send caller-selected paths or expected digests.
            parameters.pop("model_paths", None)
            expected_hashes = parameters.pop("expected_hashes", None)
            if (
                expected_hashes is not None
                and expected_hashes != signature.parameters["expected_hashes"].default
            ):
                raise ValueError("Only manifest-qualified models are supported.")
            result = runner_result(
                operation,
                inputs={"source": source_path},
                parameters=parameters,
                output_dir=destination_path.parent if destination_path else None,
                expected_outputs={"artifact": destination_path.name}
                if destination_path
                else {},
                timeout_seconds=operation_timeout(operation, parameters),
                progress_callback=progress_callback,
            )
            if not isinstance(result, dict) or "value" not in result:
                raise InvalidExtractorOutputError
            from pandect.media_runner.validation import validate_operation_result

            validate_operation_result(operation, result["value"])
            if operation in {"analyze_faces", "analyze_voice_file"}:
                from django.conf import settings

                from pandect.media_runner.validation import bind_model_provenance

                bind_model_provenance(operation, result["value"], settings.MODELS_ROOT)
            value = decode_result(get_type_hints(function)["return"], result["value"])
            if destination_path is not None and value is not None:
                if not destination_path.is_file():
                    raise InvalidExtractorOutputError
                if is_dataclass(value) and hasattr(value, "sha256"):
                    from pandect.processing.extractors.hashing import fingerprint_file

                    fingerprint = fingerprint_file(destination_path)
                    changes = {
                        "sha256": fingerprint.sha256,
                        "size_bytes": fingerprint.size_bytes,
                    }
                    if hasattr(value, "path"):
                        changes["path"] = str(destination_path)
                    value = replace(value, **changes)
            return value

        return call

    return decorate
