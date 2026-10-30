"""Inspect real local-model behavior on a fixed, synthetic qualification set."""

import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from analysis.errors import AnalysisError
from analysis.execution import snapshot_execution
from analysis.local import LocalClient
from analysis.media import transcription
from analysis.operations import get_operation
from analysis.parsers import run_parser
from analysis.sources import Source

MAX_AUDIO_BYTES = 16 * 1024 * 1024


def _id(label):
    return uuid5(NAMESPACE_URL, "pandect-local-qualification-v1/" + label)


def synthetic_sources():
    """These virtual sources never create case, person or evidence records."""
    messages = (
        "I plan to meet at Cedar Café on Friday, but I have not booked anything.",
        "Cancel Friday's Cedar Café meeting. We will not meet there.",
        "Morgan said the red van arrived; I did not see it myself.",
        "Which place do you mean, Cedar Café or Oak Library?",
        "That place might work another day; nothing is agreed.",
        "Someone mentioned a place, but the speaker and location are unclear.",
    )
    sources = []
    for index, body in enumerate(messages):
        kind = "phone-call" if index == len(messages) - 1 else "text-message"
        item = SimpleNamespace(
            pk=_id(f"item-{index}"),
            case_id=_id("case"),
            sender_id=_id("person-a" if index % 2 == 0 else "person-b"),
            recipient_id=_id("person-b" if index % 2 == 0 else "person-a"),
            occurred_at=datetime(2026, 1, 5, 12, tzinfo=UTC) + timedelta(minutes=index),
            title=f"Synthetic exchange {index + 1}",
            body=body,
        )
        sources.append(Source(str(_id(f"source-{index}")), kind, item))
    return sources


class Command(BaseCommand):
    help = (
        "Evaluate private local models on synthetic conversations without saving "
        "evidence. Optional --audio must name synthetic or sanitized PCM WAV. "
        "Prints JSON with source anchors, expected distinctions and runtime usage."
    )
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument(
            "--method",
            dest="methods",
            action="append",
            choices=(
                "text_topics",
                "session_summaries",
                "content_events",
                "conversation_link",
                "transcription",
            ),
            help="Evaluate only this method; repeat to select several methods.",
        )
        parser.add_argument(
            "--audio",
            metavar="SYNTHETIC_WAV",
            help="Supply synthetic/sanitized WAV, at most 90 seconds/16 MiB.",
        )

    def handle(self, *args, **options):
        selected = set(options["methods"] or ())
        if "transcription" in selected and not options["audio"]:
            raise CommandError("--method transcription requires --audio SYNTHETIC_WAV.")
        sources = synthetic_sources()
        report = {
            "fixture_version": "local-qualification-v1",
            "limitations": [
                "This fixture does not measure general investigation accuracy.",
                "Completed means the operation and anchor validation ran; "
                "it is not a model-quality grade.",
                "Inspect output against the expected distinctions. "
                "No cloud service is used and no evidence records are saved.",
            ],
            "sources": [
                {"id": source.id, "kind": source.kind, "text": source.text}
                for source in sources
            ],
            "expected_distinctions": [
                "The Friday meeting was proposed, not booked, and then cancelled; "
                "do not describe an observed/completed meeting.",
                "The red-van arrival is attributed hearsay from Morgan, "
                "not a firsthand observation or established event.",
                "'That place' has at least two competing referents; "
                "do not silently choose Cedar Café or Oak Library.",
                "The call transcript does not resolve a speaker, location, "
                "or connection to the proposed meeting.",
                "Every model suggestion must retain exact source quotations; "
                "a semantic link needs citations to both sides.",
            ],
            "methods": {},
        }
        for name, parameters in (
            ("text_topics", {"include_summary": True}),
            ("session_summaries", {}),
            ("content_events", {}),
            ("conversation_link", {"semantic_links": True}),
        ):
            if selected and name not in selected:
                report["methods"][name] = {
                    "status": "not_run",
                    "reason": "Method was not selected.",
                }
                continue
            report["methods"][name] = self._evaluate(
                name,
                parameters,
                lambda client, name=name, parameters=parameters: get_operation(
                    name
                ).function(sources, parameters, infer=client.infer),
            )
        if options["audio"] and (not selected or "transcription" in selected):
            report["methods"]["transcription"] = self._evaluate(
                "transcription",
                {"language": "en"},
                lambda client: self._audio(options["audio"], client),
            )
        else:
            report["methods"]["transcription"] = {
                "status": "not_run",
                "reason": "Method was not selected."
                if selected
                else (
                    "Supply --audio with a synthetic or sanitized WAV "
                    "to evaluate speech."
                ),
            }
        report["summary"] = {
            state: [
                name
                for name, result in report["methods"].items()
                if result["status"] == state
            ]
            for state in ("completed", "failed", "not_run")
        }
        self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
        if report["summary"]["failed"]:
            raise CommandError(
                "Local qualification failures are recorded in the JSON report."
            )

    def _evaluate(self, name, parameters, operation):
        started = time.monotonic()
        client = None
        execution = snapshot_execution(name, parameters | {"backend": "local"})
        result = {
            "execution": {
                key: execution.get(key)
                for key in (
                    "backend",
                    "purpose",
                    "model",
                    "sha256",
                    "runtime",
                    "adapter_version",
                    "inference_settings",
                    "context_tokens",
                    "max_output_tokens",
                )
                if key in execution
            }
        }
        try:
            client = LocalClient(lambda: None, execution)
            result |= {"status": "completed", "output": operation(client)}
        except AnalysisError as error:
            result |= {"status": "failed", "error_code": error.code}
        except ValidationError as error:
            citation_error = any(
                term in message.lower()
                for message in error.messages
                for term in ("citation", "quotation")
            )
            result |= {
                "status": "failed",
                "error_code": "citation_validation_rejected"
                if citation_error
                else "operation_validation_rejected",
            }
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        result["usage"] = client.usage if client else None
        return result

    def _audio(self, filename, client):
        try:
            with Path(filename).open("rb") as stream:
                content = stream.read(MAX_AUDIO_BYTES + 1)
        except OSError:
            raise AnalysisError("qualification_audio_unavailable") from None
        if len(content) > MAX_AUDIO_BYTES:
            raise AnalysisError("input_limit")
        metadata = run_parser("audio", content)
        if metadata["status"] != "complete":
            raise AnalysisError("qualification_audio_unsupported")
        if metadata["frames"] / metadata["sample_rate"] > 90:
            raise AnalysisError("qualification_audio_duration_limit")
        # Reuse the ordinary checksum/parser/chunk/interval workflow against
        # read-only in-memory storage, without a database or evidence upload.
        original = SimpleNamespace(
            name="synthetic-qualification.wav",
            storage=SimpleNamespace(open=lambda *args: BytesIO(content)),
        )
        item = SimpleNamespace(
            pk=_id("audio"),
            case_id=_id("case"),
            title="Operator-supplied synthetic/sanitized qualification recording",
            file=original,
            byte_size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )
        source = Source(str(_id("audio-source")), "audio", item)
        return transcription(
            [source], {"language": "en"}, gateway=client, authorize=lambda: None
        )
