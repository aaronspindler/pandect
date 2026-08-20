"""Report local processing and optional remote capability availability."""

import shutil

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from pandect.processing.cloudflare_diagnostics import (
    CloudflareDiagnosticError,
    diagnose_cloudflare,
)
from pandect.processing.extractors.errors import ExtractorError
from pandect.processing.extractors.subprocesses import run_command
from pandect.processing.provisioning import (
    check_models,
    check_work_storage,
    smoke_infer_models_isolated,
)
from pandect.processing.remote.cloudflare import CloudflareConfig

TOOL_VERSION_COMMANDS = {
    "ffmpeg": ("ffmpeg", "-version"),
    "ffprobe": ("ffprobe", "-version"),
    "exiftool": ("exiftool", "-ver"),
    "tesseract": ("tesseract", "--version"),
}


def _tool_version(argv: tuple[str, ...]) -> str | None:
    try:
        result = run_command(argv, timeout_seconds=15, output_limit_bytes=64 * 1024)
    except ExtractorError:
        return None
    combined = result.stdout + b"\n" + result.stderr
    for raw_line in combined.decode("utf-8", errors="replace").splitlines():
        line = " ".join(raw_line.strip().split())
        if line:
            return line[:200]
    return None


class Command(BaseCommand):
    help = "Inspect local processing tools, models, and Cloudflare configuration."

    def add_arguments(self, parser):
        parser.add_argument("--smoke-models", action="store_true")
        parser.add_argument("--cloudflare", action="store_true")
        parser.add_argument(
            "--accept-meta-license",
            action="store_true",
            help=(
                "Explicitly accept/reaffirm the configured Meta model license and "
                "acceptable-use policy before the evidence-free remote smoke test."
            ),
        )

    def handle(self, *args, **options):
        if options["accept_meta_license"] and not options["cloudflare"]:
            raise CommandError("--accept-meta-license requires --cloudflare.")
        unavailable = []
        for binary, argv in TOOL_VERSION_COMMANDS.items():
            version = _tool_version(argv) if shutil.which(binary) else None
            if version is None:
                unavailable.append(binary)
                self.stdout.write(f"tool {binary}: unavailable")
            else:
                self.stdout.write(f"tool {binary}: available ({version})")

        model_checks = check_models(settings.MODELS_ROOT)
        for check in model_checks:
            state = "valid" if check.valid else "missing"
            if check.present and not check.valid:
                state = "checksum mismatch"
            self.stdout.write(f"model {check.artifact.name}: {state}")
            if options["smoke_models"] and check.artifact.required and not check.valid:
                unavailable.append(check.artifact.name)

        storage = check_work_storage(settings.WORK_ROOT, settings.WORK_ROOT)
        self.stdout.write(
            "work storage: "
            + ("writable and atomic" if storage.valid else storage.error_code)
        )
        if not storage.valid:
            unavailable.append("work storage")

        if options["smoke_models"]:
            for check in smoke_infer_models_isolated(settings.MODELS_ROOT):
                state = "inference passed" if check.passed else check.error_code
                self.stdout.write(f"model runtime {check.component}: {state}")
                if not check.passed:
                    unavailable.append(check.component)

        self.stdout.write(
            "Cloudflare AI: "
            + ("enabled" if settings.CLOUDFLARE_AI_ENABLED else "disabled")
        )
        if options["cloudflare"]:
            try:
                report = diagnose_cloudflare(
                    CloudflareConfig.from_settings(),
                    accept_meta_license=options["accept_meta_license"],
                )
            except CloudflareDiagnosticError as error:
                message = f"Cloudflare diagnostic failed ({error.code})."
                if error.code == "gateway_or_model_execution_failed":
                    message += (
                        " If the configured Meta model license has not been accepted, "
                        "review its terms and rerun with --accept-meta-license."
                    )
                raise CommandError(message) from error
            self.stdout.write("Cloudflare credentials: valid")
            self.stdout.write("Cloudflare Gateway: reachable")
            for model in report.models_available:
                self.stdout.write(f"Cloudflare model {model}: available")
            self.stdout.write(f"Meta license action: {report.meta_license_action}")

        if options["smoke_models"] and unavailable:
            raise CommandError("Required processing capabilities are unavailable.")
