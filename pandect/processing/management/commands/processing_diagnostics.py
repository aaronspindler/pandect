"""Report local processing and optional remote capability availability."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from pandect.media_runner.client import status as media_runner_status
from pandect.media_runner.protocol import RunnerBusy, RunnerError, RunnerJobError
from pandect.processing.cloudflare_diagnostics import (
    CloudflareDiagnosticError,
    diagnose_cloudflare,
)
from pandect.processing.extractors.errors import ExtractorError
from pandect.processing.provisioning import (
    check_models,
    check_work_storage,
    smoke_infer_models_isolated,
)
from pandect.processing.remote.cloudflare import CloudflareConfig

TOOL_NAMES = ("ffmpeg", "ffprobe", "exiftool", "tesseract")


def _tool_version(tool: str) -> str | None:
    from pandect.media_runner.client import run_operation

    try:
        result = run_operation(
            "tool_version",
            inputs={},
            parameters={"tool": tool},
            timeout_seconds=15,
        )
    except (ExtractorError, RunnerJobError):
        return None
    version = result.get("version")
    return version if isinstance(version, str) and 0 < len(version) <= 200 else None


class Command(BaseCommand):
    help = "Inspect local processing tools, models, and Cloudflare configuration."

    def report_runner_state(self, state: str) -> None:
        guidance = {
            "ready": "",
            "busy": "; retry diagnostics after the current job finishes.",
            "unavailable": (
                "; run make up or inspect docker compose logs media-runner, then retry."
            ),
        }
        self.stdout.write(f"media runner: {state}{guidance[state]}")

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
        try:
            runner_state = "busy" if media_runner_status()["busy"] else "ready"
        except RunnerBusy:
            runner_state = "busy"
        except RunnerError:
            runner_state = "unavailable"
        self.report_runner_state(runner_state)
        for binary in TOOL_NAMES:
            version = None
            if runner_state == "ready":
                try:
                    version = _tool_version(binary)
                except RunnerBusy:
                    runner_state = "busy"
                    self.report_runner_state(runner_state)
                except RunnerError:
                    runner_state = "unavailable"
                    self.report_runner_state(runner_state)
            if runner_state != "ready":
                self.stdout.write(
                    f"tool {binary}: not checked (media runner {runner_state})"
                )
                continue
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

        if options["smoke_models"] and runner_state != "ready":
            unavailable.append("media runner")
            self.stdout.write(
                f"model runtime: not checked (media runner {runner_state})"
            )
        elif options["smoke_models"]:
            for check in smoke_infer_models_isolated(settings.MODELS_ROOT):
                state = "inference passed" if check.passed else check.error_code
                self.stdout.write(f"model runtime {check.component}: {state}")
                if not check.passed:
                    unavailable.append(check.component)
                    if check.error_code == "media_runner_failed":
                        self.stdout.write(
                            "Model smoke could not use the media runner. Retry after "
                            "the current job finishes, or run make up and inspect "
                            "docker compose logs media-runner."
                        )

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
