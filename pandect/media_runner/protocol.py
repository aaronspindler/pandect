"""Small, non-executable wire format shared by trusted runner peers.

Only this module and the standard library are needed before child confinement.
Both directions use a four-byte length-prefixed JSON object followed by the exact
declared artifact bytes. No supplied name is ever interpreted as a filesystem path.
"""

import json
import math
import re
import socket
import struct
import time

VERSION = 1
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_INPUT_BYTES = 2 * 1024**3
MAX_OUTPUT_BYTES = 1024**3
MAX_FILES = 1024
MAX_INPUT_FILES = 128
MAX_TIMEOUT_SECONDS = 7200
CHUNK_BYTES = 1024 * 1024
ERROR_CODES = frozenset(
    {
        "job_failed",
        "cancelled",
        "extractor_error",
        "tool_unavailable",
        "optional_dependency_unavailable",
        "local_model_unavailable",
        "local_model_integrity",
        "tool_timed_out",
        "tool_output_limit",
        "tool_failed",
        "invalid_tool_output",
        "resource_limit",
        "unsupported_pixel_format",
    }
)
OPERATION_NAMES = frozenset(
    {
        "probe_media",
        "extract_metadata",
        "extract_ocr",
        "analyze_image",
        "render_image_frame",
        "detect_silence",
        "analyze_acoustics",
        "extract_audio_chunk",
        "detect_scene_changes",
        "extract_video_frame",
        "extract_normalized_audio_stream",
        "extract_faithful_audio_stream",
        "extract_normalized_audio_chunk",
        "extract_embedded_artwork",
        "extract_subtitle_stream",
        "analyze_faces",
        "analyze_voice_file",
        "decode_qr",
        "tool_version",
        "model_smoke",
    }
)
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z", re.ASCII)


class RunnerError(Exception):
    """A safe-to-display media-runner error, never containing source data."""


class RunnerBusy(RunnerError):
    pass


class RunnerUnavailable(RunnerError):
    pass


class RunnerProtocolError(RunnerError):
    pass


class RunnerJobError(RunnerError):
    def __init__(self, message, *, code="job_failed"):
        super().__init__(message)
        self.code = code


def safe_name(value):
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise RunnerProtocolError("The media runner returned an invalid file name.")
    return value


def safe_error_code(value):
    # Even an ASCII-only parser-supplied string could contain evidence or secrets.
    return value if isinstance(value, str) and value in ERROR_CODES else "job_failed"


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RunnerProtocolError("Duplicate media-runner JSON fields.")
        result[key] = value
    return result


def validate_json(value, *, depth=0, budget=None):
    if budget is None:
        budget = [200_000]
    budget[0] -= 1
    if depth > 32 or budget[0] < 0:
        raise RunnerProtocolError("Media-runner JSON is too complex.")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    if isinstance(value, list):
        for item in value:
            validate_json(item, depth=depth + 1, budget=budget)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            validate_json(item, depth=depth + 1, budget=budget)
        return
    raise RunnerProtocolError("Invalid media-runner JSON value.")


def encode_json(value):
    validate_json(value)
    data = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
    if len(data) > MAX_JSON_BYTES:
        raise RunnerProtocolError("Media-runner JSON exceeds its size limit.")
    return data


def decode_json(data):
    if len(data) > MAX_JSON_BYTES:
        raise RunnerProtocolError("Media-runner JSON exceeds its size limit.")
    try:
        value = json.loads(data, object_pairs_hook=_unique_object)
        validate_json(value)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise RunnerProtocolError("Invalid media-runner JSON.") from exc
    if not isinstance(value, dict):
        raise RunnerProtocolError("Media-runner JSON must be an object.")
    return value


def receive_exact(connection, size, *, deadline=None):
    chunks = bytearray()
    while len(chunks) < size:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("The media-runner transfer deadline expired.")
            connection.settimeout(remaining)
        chunk = connection.recv(min(CHUNK_BYTES, size - len(chunks)))
        if not chunk:
            raise RunnerProtocolError("The media runner connection ended early.")
        chunks.extend(chunk)
    return bytes(chunks)


def receive_json(connection, *, deadline=None):
    length = struct.unpack("!I", receive_exact(connection, 4, deadline=deadline))[0]
    if length > MAX_JSON_BYTES or length == 0:
        raise RunnerProtocolError("Media-runner JSON exceeds its size limit.")
    return decode_json(receive_exact(connection, length, deadline=deadline))


def send_json(connection, value):
    data = encode_json(value)
    connection.sendall(struct.pack("!I", len(data)) + data)


def file_manifest(value, *, output=False):
    limit = MAX_OUTPUT_BYTES if output else MAX_INPUT_BYTES
    count_limit = MAX_FILES if output else MAX_INPUT_FILES
    if not isinstance(value, list) or len(value) > count_limit:
        raise RunnerProtocolError("Invalid media-runner file count.")
    seen = set()
    logical_names = set()
    total = 0
    for entry in value:
        fields = {"name", "size", "key"} if output else {"name", "size"}
        if not isinstance(entry, dict) or set(entry) != fields:
            raise RunnerProtocolError("Invalid media-runner file manifest.")
        name = safe_name(entry["name"])
        if name == "result.json" or name in seen:
            raise RunnerProtocolError("Duplicate or reserved media-runner file name.")
        seen.add(name)
        if output:
            key = safe_name(entry["key"])
            if key in logical_names:
                raise RunnerProtocolError("Duplicate media-runner output key.")
            logical_names.add(key)
        size = entry["size"]
        if type(size) is not int or size < 0 or size > limit:
            raise RunnerProtocolError("Invalid media-runner file size.")
        total += size
        if total > limit:
            raise RunnerProtocolError("Media-runner files exceed their size limit.")
    return value


def validate_request(request):
    if set(request) != {
        "version",
        "operation",
        "parameters",
        "inputs",
        "timeout_seconds",
    }:
        raise RunnerProtocolError("Invalid media-runner request fields.")
    if type(request["version"]) is not int or request["version"] != VERSION:
        raise RunnerProtocolError("Unsupported media-runner protocol version.")
    if (
        not isinstance(request["operation"], str)
        or request["operation"] not in OPERATION_NAMES
    ):
        raise RunnerProtocolError("Unsupported media-runner operation.")
    if not isinstance(request["parameters"], dict):
        raise RunnerProtocolError("Media-runner parameters must be an object.")
    timeout = request["timeout_seconds"]
    if (
        type(timeout) not in (int, float)
        or not math.isfinite(timeout)
        or not 0 < timeout <= MAX_TIMEOUT_SECONDS
    ):
        raise RunnerProtocolError("Invalid media-runner deadline.")
    file_manifest(request["inputs"])
    validate_json(request)
    return request


def peer_uid(connection):
    """Linux credentials are kernel supplied, not client JSON claims."""
    if not hasattr(socket, "SO_PEERCRED"):
        raise RunnerUnavailable("The media runner requires Linux peer credentials.")
    raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    _pid, uid, _gid = struct.unpack("3i", raw)
    return uid
