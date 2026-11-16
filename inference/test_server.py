"""Runtime boundaries with synthetic model bytes and no downloaded models."""

import base64
import hashlib
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from inference.server import Handler, Runtime, ThreadingHTTPServer


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.model = Path(self.directory.name) / "model"
        self.model.write_bytes(b"synthetic model")
        self.digest = hashlib.sha256(self.model.read_bytes()).hexdigest()
        environment = {
            "PANDECT_RUNTIME_MODEL": "synthetic-model",
            "PANDECT_RUNTIME_MODEL_SHA256": self.digest,
            "PANDECT_LOCAL_AI_TOKEN": "synthetic-runtime-secret",
        }
        self.environment = patch.dict(os.environ, environment, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.runtime = Runtime()
        self.runtime.model_path = self.model

    def test_identity_uses_measured_model_digest_and_pinned_engine(self):
        self.runtime.verify_model()
        self.assertEqual(self.runtime.identity["sha256"], self.digest)
        self.assertEqual(self.runtime.identity["kind"], "transcription")
        self.assertEqual(
            self.runtime.identity["runtime"],
            "whisper.cpp@d1be6fde11ac6e0407606b4e42fe72d34add8037",
        )

    def test_retired_environment_options_cannot_change_runtime_bounds(self):
        names = (
            "PANDECT_RUNTIME_TIMEOUT",
            "PANDECT_RUNTIME_MAX_REQUEST_BYTES",
            "PANDECT_RUNTIME_MAX_RESPONSE_BYTES",
            "PANDECT_RUNTIME_THREADS",
        )
        for value in ("1", "not-an-integer"):
            with (
                self.subTest(value=value),
                patch.dict(os.environ, dict.fromkeys(names, value)),
            ):
                runtime = Runtime()
                self.assertEqual(runtime.timeout, 600)
                self.assertEqual(runtime.max_request, 8 * 1024 * 1024)
                self.assertEqual(runtime.max_response, 2 * 1024 * 1024)
                command = runtime.command()
                self.assertEqual(command[command.index("--threads") + 1], "4")

    def test_missing_or_changed_model_fails_before_engine_launch(self):
        self.model.write_bytes(b"different model")
        with patch("inference.server.subprocess.Popen") as process:
            with self.assertRaisesRegex(ValueError, "model_digest_mismatch"):
                self.runtime.start()
        process.assert_not_called()
        self.model.unlink()
        with self.assertRaisesRegex(ValueError, "model_unavailable"):
            self.runtime.verify_model()

    def test_replaced_path_cannot_inherit_the_previous_files_digest(self):
        replacement = self.model.with_name("replacement")
        replacement.write_bytes(b"different model")
        file_digest = hashlib.file_digest

        def replace_while_hashing(stream, algorithm):
            result = file_digest(stream, algorithm)
            replacement.replace(self.model)
            return result

        with (
            patch(
                "inference.server.hashlib.file_digest",
                side_effect=replace_while_hashing,
            ),
            patch("inference.server.subprocess.Popen") as process,
            self.assertRaisesRegex(ValueError, "model_changed_during_verification"),
        ):
            self.runtime.start()
        process.assert_not_called()

    def test_native_process_has_no_operator_secrets_or_output_logging(self):
        with patch("inference.server.subprocess.Popen") as process:
            self.runtime.start()
        options = process.call_args.kwargs
        self.assertNotIn("PANDECT_LOCAL_AI_TOKEN", options["env"])
        self.assertEqual(options["stdout"], subprocess.DEVNULL)
        self.assertEqual(options["stderr"], subprocess.DEVNULL)
        self.assertTrue(options["start_new_session"])
        self.assertIn("--processors", process.call_args.args[0])

    def test_changed_mounted_file_invalidates_running_runtime(self):
        self.runtime.verify_model()
        self.runtime.process = Mock()
        self.runtime.process.poll.return_value = None
        self.model.write_bytes(b"changed model bytes")
        with patch.object(self.runtime, "stop") as stop:
            self.assertFalse(self.runtime.ready())
        stop.assert_called_once()

    def test_missing_mounted_file_stops_the_running_engine(self):
        self.runtime.verify_model()
        self.runtime.process = Mock()
        self.runtime.process.poll.return_value = None
        self.model.unlink()
        with patch.object(self.runtime, "stop") as stop:
            self.assertFalse(self.runtime.ready())
        stop.assert_called_once()

    def test_stop_kills_and_reaps_real_native_process_group(self):
        self.runtime.process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        self.addCleanup(self.runtime.stop)
        self.runtime.stop()
        self.assertIsNotNone(self.runtime.process.returncode)

    def test_deadline_stops_engine_and_rejects_late_response(self):
        self.runtime.timeout = 0.02
        closed = threading.Event()
        response = Mock(status=200)
        response.read.side_effect = lambda _: (closed.wait(1), b"{}")[1]
        connection = Mock()
        connection.getresponse.return_value = response
        connection.close.side_effect = closed.set
        with (
            patch(
                "inference.server.http.client.HTTPConnection", return_value=connection
            ),
            patch.object(self.runtime, "stop") as stop,
            self.assertRaises(TimeoutError),
        ):
            self.runtime.forward("/inference", b"{}", "application/json")
        stop.assert_called_once()

    def test_whisper_command_keeps_english_and_context_boundaries(self):
        command = self.runtime.command()
        # The pinned server enables no_context by default; unlike its help text,
        # its actual CLI parser does not recognize --no-context.
        for option in ("--no-gpu", "--no-fallback"):
            self.assertIn(option, command)
        self.assertNotIn("--no-context", command)
        self.assertNotIn("--convert", command)
        self.assertEqual(command[command.index("--language") + 1], "en")

    def test_gpu_offload_is_explicit_for_a_separately_qualified_image(self):
        with patch.dict(os.environ, {"PANDECT_RUNTIME_GPU": "true"}):
            self.assertNotIn("--no-gpu", Runtime().command())

    def test_token_and_valid_model_hash_are_required(self):
        for key, value in (
            ("PANDECT_LOCAL_AI_TOKEN", ""),
            ("PANDECT_RUNTIME_MODEL_SHA256", "bad"),
        ):
            with (
                self.subTest(key=key),
                patch.dict(os.environ, {key: value}),
                self.assertRaises(ValueError),
            ):
                Runtime()


class RuntimeHTTPTests(unittest.TestCase):
    def setUp(self):
        self.runtime = Mock()
        self.runtime.token = "synthetic-secret"
        self.runtime.identity = {"kind": "transcription", "sha256": "a" * 64}
        self.runtime.max_request = 32
        self.runtime.lock = threading.Lock()
        self.runtime.ready.return_value = True
        self.runtime.forward.return_value = b'{"result":"candidate"}'
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.runtime = self.runtime
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(
        self, method, path, body=None, *, token="synthetic-secret", headers=None
    ):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        supplied = {
            "Authorization": "Bearer " + token,
            "X-Pandect-Identity": base64.urlsafe_b64encode(
                json.dumps(self.runtime.identity).encode()
            ).decode(),
            **(headers or {}),
        }
        connection.request(method, path, body=body, headers=supplied)
        response = connection.getresponse()
        value = response.status, json.loads(response.read())
        connection.close()
        return value

    def test_identity_requires_authentication_and_has_no_native_details_on_failure(
        self,
    ):
        status, value = self.request("GET", "/identity", token="wrong")
        self.assertEqual(status, 401)
        self.assertEqual(value, {"error": "authentication_required"})
        self.assertEqual(self.request("GET", "/identity"), (200, self.runtime.identity))

    def test_only_allowed_native_routes_can_receive_requests(self):
        for path in (
            "/completion",
            "/apply-template",
            "/tokenize",
            "/v1/chat/completions",
            "/inference?secret=x",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.request("POST", path, b"{}")[0], 404)
        self.runtime.forward.assert_not_called()
        status, _ = self.request("POST", "/inference", b"{}")
        self.assertEqual(status, 200)
        self.runtime.forward.assert_called_once_with(
            "/inference", b"{}", "application/json"
        )

    def test_request_limit_and_busy_runtime_do_not_forward_evidence(self):
        self.assertEqual(self.request("POST", "/inference", b"x" * 33)[0], 413)
        with self.runtime.lock:
            self.assertEqual(self.request("POST", "/inference", b"{}")[0], 503)
        self.runtime.forward.assert_not_called()

    def test_request_identity_rejects_same_alias_with_a_different_artifact(self):
        selected = self.runtime.identity | {"sha256": "b" * 64}
        identity = base64.urlsafe_b64encode(json.dumps(selected).encode()).decode()
        status, value = self.request(
            "POST", "/inference", b"{}", headers={"X-Pandect-Identity": identity}
        )
        self.assertEqual(status, 409)
        self.assertEqual(value, {"error": "runtime_identity_changed"})
        self.runtime.forward.assert_not_called()

    def test_missing_or_malformed_request_identity_does_not_reach_engine(self):
        for value in ("", "not-base64!", "x" * 4097):
            with self.subTest(value=value[:20]):
                self.assertEqual(
                    self.request(
                        "POST",
                        "/inference",
                        b"{}",
                        headers={"X-Pandect-Identity": value},
                    )[0],
                    400,
                )
        self.runtime.forward.assert_not_called()

    def test_error_bodies_never_relay_native_exception_strings(self):
        self.runtime.forward.side_effect = ValueError("private source or model path")
        status, value = self.request("POST", "/inference", b"{}")
        self.assertEqual(status, 503)
        self.assertEqual(value, {"error": "runtime_unavailable"})
        # The client can read the response before the handler's finally block runs.
        self.assertTrue(self.runtime.lock.acquire(timeout=2))
        self.runtime.lock.release()


if __name__ == "__main__":
    unittest.main()
