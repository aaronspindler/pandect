"""Private, bounded front door for the two pinned native inference servers."""

import base64
import hashlib
import hmac
import http.client
import json
import os
import re
import signal
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def positive(name, default):
    value = int(os.environ.get(name, default))
    if value <= 0:
        raise ValueError("invalid_runtime_configuration")
    return value


def model_signature(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


class Runtime:
    def __init__(self):
        self.kind = os.environ["PANDECT_RUNTIME_KIND"]
        if self.kind not in {"text", "transcription"}:
            raise ValueError("invalid_runtime_configuration")
        revisions = json.loads(Path(__file__).with_name("revisions.json").read_text())
        self.identity = {
            "kind": self.kind,
            "runtime": revisions[self.kind],
            "model": os.environ["PANDECT_RUNTIME_MODEL"],
            "sha256": os.environ["PANDECT_RUNTIME_MODEL_SHA256"].lower(),
        }
        self.token = os.environ.get("PANDECT_LOCAL_AI_TOKEN", "")
        if not self.token or not re.fullmatch(r"[0-9a-f]{64}", self.identity["sha256"]):
            raise ValueError("invalid_runtime_configuration")
        self.model_path = Path("/models/model")
        self.timeout = min(1800, positive("PANDECT_RUNTIME_TIMEOUT", 600))
        self.max_request = positive(
            "PANDECT_RUNTIME_MAX_REQUEST_BYTES", 8 * 1024 * 1024
        )
        self.max_response = positive(
            "PANDECT_RUNTIME_MAX_RESPONSE_BYTES", 2 * 1024 * 1024
        )
        self.threads = positive("PANDECT_RUNTIME_THREADS", 4)
        self.gpu_layers = int(os.environ.get("PANDECT_RUNTIME_GPU_LAYERS", "0"))
        if self.gpu_layers < 0:
            raise ValueError("invalid_runtime_configuration")
        self.gpu = os.environ.get("PANDECT_RUNTIME_GPU", "false").lower() == "true"
        if self.kind == "text":
            self.identity["context_tokens"] = positive(
                "PANDECT_RUNTIME_CONTEXT_TOKENS", 32768
            )
        self.lock = threading.Lock()
        self.process = None
        self.verified_model = None

    def verify_model(self):
        if not self.model_path.is_file():
            raise ValueError("model_unavailable")
        with self.model_path.open("rb") as stream:
            signature = model_signature(os.fstat(stream.fileno()))
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if signature != model_signature(os.fstat(stream.fileno())):
                raise ValueError("model_changed_during_verification")
        if signature != model_signature(self.model_path.stat()):
            raise ValueError("model_changed_during_verification")
        if not hmac.compare_digest(digest, self.identity["sha256"]):
            raise ValueError("model_digest_mismatch")
        self.verified_model = signature

    def command(self):
        common = [
            "--model",
            str(self.model_path),
            "--host",
            "127.0.0.1",
            "--port",
            "8081",
        ]
        if self.kind == "text":
            return [
                "/opt/pandect/llama-server",
                *common,
                "--alias",
                self.identity["model"],
                "--ctx-size",
                str(self.identity["context_tokens"]),
                "--parallel",
                "1",
                "--threads",
                str(self.threads),
                "--n-gpu-layers",
                str(self.gpu_layers),
                "--no-webui",
                "--jinja",
                "--reasoning",
                "off",
            ]
        return [
            "/opt/pandect/whisper-server",
            *common,
            "--threads",
            str(self.threads),
            "--processors",
            "1",
            "--language",
            "en",
            "--no-fallback",
        ] + ([] if self.gpu else ["--no-gpu"])

    def start(self):
        self.verify_model()
        # Inference children receive no Django, storage, Gateway or runtime token.
        self.process = subprocess.Popen(
            self.command(),
            cwd="/tmp",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": "/tmp",
                "TMPDIR": "/tmp",
            },
            start_new_session=True,
        )

    def stop(self):
        process = self.process
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()

    def ready(self):
        if self.process is None or self.process.poll() is not None:
            return False
        try:
            current = model_signature(self.model_path.stat())
        except OSError:
            current = None
        if current is None or current != self.verified_model:
            self.stop()
            return False
        try:
            connection = http.client.HTTPConnection("127.0.0.1", 8081, timeout=2)
            try:
                connection.request("GET", "/health")
                response = connection.getresponse()
                response.read(1024)
                return response.status == 200
            finally:
                connection.close()
        except OSError, http.client.HTTPException:
            return False

    def forward(self, path, body, content_type):
        connection = http.client.HTTPConnection("127.0.0.1", 8081, timeout=self.timeout)
        expired = threading.Event()

        def timeout():
            expired.set()
            self.stop()
            connection.close()

        timer = threading.Timer(self.timeout, timeout)
        timer.daemon = True
        timer.start()
        try:
            connection.request(
                "POST", path, body=body, headers={"Content-Type": content_type}
            )
            response = connection.getresponse()
            result = response.read(self.max_response + 1)
            if expired.is_set():
                raise TimeoutError
            if response.status != 200 or len(result) > self.max_response:
                raise ValueError("invalid_engine_response")
            return result
        finally:
            timer.cancel()
            connection.close()


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.0 closes each connection, including unread bodies after rejection.
    server_version = "PandectInference"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, format, *args):
        pass

    def reply(self, status, data):
        body = json.dumps(data).encode() if not isinstance(data, bytes) else data
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        expected = ("Bearer " + self.server.runtime.token).encode()
        received = self.headers.get("Authorization", "").encode()
        if not hmac.compare_digest(received, expected):
            self.reply(401, {"error": "authentication_required"})
            return False
        return True

    def do_GET(self):
        runtime = self.server.runtime
        if self.path == "/health":
            ready = runtime.ready()
            self.reply(
                200 if ready else 503,
                {"status": "ready" if ready else "unavailable"},
            )
        elif self.path == "/identity" and self.authorized():
            if runtime.ready():
                self.reply(200, runtime.identity)
            else:
                self.reply(503, {"error": "runtime_unavailable"})
        else:
            if self.path != "/identity":
                self.reply(404, {"error": "unknown_route"})

    def do_POST(self):
        if not self.authorized():
            return
        runtime = self.server.runtime
        allowed = (
            {"/apply-template", "/tokenize", "/v1/chat/completions"}
            if runtime.kind == "text"
            else {"/inference"}
        )
        if self.path not in allowed:
            self.reply(404, {"error": "unknown_route"})
            return
        if self.headers.get("Transfer-Encoding"):
            self.reply(400, {"error": "content_length_required"})
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.reply(400, {"error": "content_length_required"})
            return
        if not 0 < length <= runtime.max_request:
            self.reply(413, {"error": "request_limit"})
            return
        if not runtime.lock.acquire(blocking=False):
            self.reply(503, {"error": "runtime_busy"})
            return
        try:
            if not runtime.ready():
                self.reply(503, {"error": "runtime_unavailable"})
                return
            encoded = self.headers.get("X-Pandect-Identity", "")
            if not encoded or len(encoded) > 4096:
                self.reply(400, {"error": "runtime_identity_required"})
                return
            try:
                expected = json.loads(
                    base64.b64decode(
                        encoded + "=" * (-len(encoded) % 4),
                        altchars=b"-_",
                        validate=True,
                    )
                )
            except ValueError, UnicodeError:
                self.reply(400, {"error": "invalid_runtime_identity"})
                return
            if expected != runtime.identity:
                self.reply(409, {"error": "runtime_identity_changed"})
                return
            body = self.rfile.read(length)
            if len(body) != length:
                self.reply(400, {"error": "incomplete_request"})
                return
            result = runtime.forward(
                self.path, body, self.headers.get("Content-Type", "application/json")
            )
            self.reply(200, result)
        except OSError, http.client.HTTPException, ValueError:
            self.reply(503, {"error": "runtime_unavailable"})
        finally:
            runtime.lock.release()


def main():
    runtime = Runtime()
    runtime.start()
    server = ThreadingHTTPServer(("0.0.0.0", 8080), Handler)
    server.runtime = runtime
    server.daemon_threads = True
    stopped = threading.Event()

    def shutdown(signum, frame):
        stopped.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        while not stopped.wait(1):
            if runtime.process.poll() is not None:
                return 1
    finally:
        runtime.stop()
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OSError, ValueError, KeyError:
        # Never expose paths, command arguments, tokens or native exception text.
        raise SystemExit(
            "Local inference runtime could not start; check operator configuration."
        ) from None
