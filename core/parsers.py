"""Bounded parser transport; domain decoding runs in an isolated child.

This resource boundary is not an operating-system security sandbox. Deployed
workers still require confinement. Child input contains bytes and options only.
"""

import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
from importlib import import_module
from pathlib import Path

# Isolated script execution omits the project root. Add only the root derived
# from this trusted runner, never a caller-supplied path or working directory.
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.parser_contract import MAX_BYTES, MAX_OUTPUT, ParserFailure

PARSER_SECONDS = 20
PARSER_MODULES = {
    "document": "document.parsers",
    "document_preview": "document.parsers",
    "image": "image.parsers",
    "ocr": "image.analysis.ai_parser",
    "plate_ocr": "image.analysis.plate_parser",
    "image_preview": "image.parsers",
    "audio": "audio.parsers",
    "audio_preview": "audio.parsers",
    "audio_chunk": "audio.parsers",
    "geographic_context": "geo.parsers",
}


def read_source(source):
    """Read through storage, checking the applicable original file-byte hash."""
    from django.conf import settings

    from core.analysis.errors import AnalysisError
    from core.storage import open_private_file

    record = source.attachment if source.attachment is not None else source.item
    original = getattr(record, "file", None)
    if not original:
        raise AnalysisError("retained_file_required")
    maximum = min(MAX_BYTES, settings.PANDECT_ANALYSIS_MAX_BYTES)
    if getattr(record, "byte_size", maximum + 1) > maximum:
        raise AnalysisError("input_limit")
    expected = record.audio_sha256 if source.attachment is not None else record.sha256
    try:
        with open_private_file(record, source.item.case_id) as stream:
            content = stream.read(maximum + 1)
    except OSError:
        raise AnalysisError("source_unavailable") from None
    if len(content) > maximum:
        raise AnalysisError("input_limit")
    if (
        len(content) != record.byte_size
        or hashlib.sha256(content).hexdigest() != expected
    ):
        raise AnalysisError("source_changed")
    return content, expected


def run_parser(operation, content, *, options=None):
    """Keep risky decoding outside the worker and bound wall time and output."""
    from core.analysis.errors import AnalysisError

    if operation not in PARSER_MODULES:
        raise AnalysisError("unsupported_parser")
    if not isinstance(content, bytes) or len(content) > MAX_BYTES:
        raise AnalysisError("input_limit")
    # The child's file-size limit applies to a regular file, while a pipe
    # would let communicate() buffer stdout before checking its length.
    try:
        output = tempfile.TemporaryFile()
    except OSError:
        raise AnalysisError("parser_unavailable") from None
    with output:
        _run_child(operation, content, options, output)
        output.seek(0)
        stdout = output.read(MAX_OUTPUT + 1)
    if len(stdout) > MAX_OUTPUT:
        raise AnalysisError("parser_resource_limit")
    try:
        value = json.loads(stdout)
    except ValueError, UnicodeError:
        raise AnalysisError("parser_failed") from None
    if not isinstance(value, dict):
        raise AnalysisError("parser_failed")
    if "error" in value:
        raise AnalysisError(value["error"])
    return value


def _run_child(operation, content, options, output):
    from core.analysis.errors import AnalysisError

    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                str(Path(__file__).resolve()),
                operation,
                json.dumps(options or {}),
            ],
            start_new_session=True,
            stdin=subprocess.PIPE,
            stdout=output,
            stderr=subprocess.DEVNULL,
            env={
                **{
                    key: value
                    for key, value in os.environ.items()
                    if key in {"PATH", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP"}
                },
                # Native geometry dependencies must not create an unbounded
                # numerical-library thread pool inside the bounded child.
                "OPENBLAS_NUM_THREADS": "1",
                "OMP_NUM_THREADS": "1",
            },
        )
    except OSError:
        raise AnalysisError("parser_unavailable") from None
    deadline = time.monotonic() + PARSER_SECONDS
    try:
        pending = content
        while True:
            try:
                process.communicate(input=pending, timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                pending = None
                if time.monotonic() > deadline:
                    raise AnalysisError("parser_timeout") from None
                if sys.platform == "darwin":
                    usage = subprocess.run(
                        ["/bin/ps", "-axo", "pgid=,rss="],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        timeout=1,
                        check=False,
                    )
                    if (
                        usage.returncode == 0
                        and sum(
                            int(row.split()[1])
                            for row in usage.stdout.splitlines()
                            if row.split()[0] == str(process.pid).encode()
                        )
                        > 1024**2
                    ):
                        raise AnalysisError("parser_resource_limit") from None
    finally:
        # Reap native OCR children too, including after a parser crash.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
    if process.returncode != 0:
        raise AnalysisError("parser_resource_limit")


def _child():
    import resource

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT, MAX_OUTPUT))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    if sys.platform == "linux":
        resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
    logging.disable(logging.CRITICAL)
    content = sys.stdin.buffer.read(MAX_BYTES + 1)
    try:
        if len(content) > MAX_BYTES:
            raise ParserFailure("input_limit")
        operation = sys.argv[1]
        module_name = PARSER_MODULES.get(operation)
        if module_name is None:
            raise ParserFailure("unsupported_parser")
        parser = import_module(module_name)
        value = parser.parse(operation, content, json.loads(sys.argv[2]))
        result = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        if len(result) > MAX_OUTPUT:
            raise ParserFailure("parser_output_limit")
    except ParserFailure as error:
        result = json.dumps({"error": str(error)}).encode()
    except MemoryError:
        result = b'{"error":"parser_resource_limit"}'
    except Exception:
        result = b'{"error":"malformed_input"}'
    sys.stdout.buffer.write(result)


if __name__ == "__main__":
    _child()
