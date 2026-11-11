"""Domain parsers load in isolated Python without application initialization."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from core.analysis.errors import AnalysisError
from core.parsers import run_parser


class IsolatedDomainParserTests(SimpleTestCase):
    def test_domain_parsers_import_without_django_or_environment_configuration(self):
        project_root = str(Path(__file__).resolve().parents[3])
        script = """
import importlib
import json
import sys
sys.path.insert(0, sys.argv[1])
from core.parsers import PARSER_MODULES
for name in sorted(set(PARSER_MODULES.values())):
    importlib.import_module(name)
assert not any(name == 'django' or name.startswith('django.') for name in sys.modules)
print(json.dumps({'django_loaded': False}))
"""
        with tempfile.TemporaryDirectory(prefix="pandect-parser-import-") as cwd:
            result = subprocess.run(
                [sys.executable, "-I", "-c", script, project_root],
                cwd=cwd,
                env={},
                capture_output=True,
                check=True,
                timeout=10,
            )
        self.assertEqual(json.loads(result.stdout), {"django_loaded": False})

    def test_runner_dispatch_works_outside_the_project_directory(self):
        runner = Path(__file__).resolve().parents[2] / "parsers.py"
        with tempfile.TemporaryDirectory(prefix="pandect-parser-child-") as cwd:
            result = subprocess.run(
                [sys.executable, "-I", str(runner), "document", "{}"],
                cwd=cwd,
                env={},
                input=b"Synthetic retained text",
                capture_output=True,
                check=True,
                timeout=10,
            )
        parsed = json.loads(result.stdout)
        self.assertEqual(parsed["status"], "complete")
        self.assertEqual(parsed["text"], "Synthetic retained text")

    def test_worker_checks_output_size_before_decoding(self):
        def start_child(*args, stdout, **kwargs):
            stdout.write(b'{"status":"complete"}')
            return Mock(returncode=0)

        with (
            patch("core.parsers.MAX_OUTPUT", 16),
            patch("core.parsers.os.killpg"),
            patch("core.parsers.subprocess.Popen", side_effect=start_child) as child,
            patch("core.parsers.json.loads") as decode,
            self.assertRaisesMessage(AnalysisError, "parser_resource_limit"),
        ):
            run_parser("document", b"Synthetic retained text")

        decode.assert_not_called()
        self.assertTrue(child.call_args.kwargs["stdout"].closed)
