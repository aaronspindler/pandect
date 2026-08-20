"""Explicitly download and verify the pinned local processing model set."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from pandect.processing.model_manifest import MODEL_ARTIFACTS
from pandect.processing.provisioning import ProvisioningError, provision_models


class Command(BaseCommand):
    help = "Provision pinned local processing models after explicit license acceptance."

    def add_arguments(self, parser):
        parser.add_argument(
            "--accept-licenses",
            action="store_true",
            help="Confirm review and acceptance of every selected model license.",
        )
        parser.add_argument(
            "--artifact",
            action="append",
            choices=tuple(artifact.filename for artifact in MODEL_ARTIFACTS),
            dest="artifacts",
            help="Provision only this manifest filename; may be repeated.",
        )
        parser.add_argument(
            "--replace-invalid",
            action="store_true",
            help="Atomically replace a present file that fails its pinned checksum.",
        )
        parser.add_argument(
            "--list",
            action="store_true",
            help="List the pinned manifest and licenses without downloading anything.",
        )

    def handle(self, *args, **options):
        selected_names = set(options["artifacts"] or ())
        selected = tuple(
            artifact
            for artifact in MODEL_ARTIFACTS
            if not selected_names or artifact.filename in selected_names
        )
        if options["list"]:
            for artifact in selected:
                self.stdout.write(
                    f"{artifact.filename}: {artifact.revision}; "
                    f"license={artifact.license}; sha256={artifact.sha256}; "
                    f"source={artifact.source}"
                )
                self.stdout.write(
                    "  training-corpus limitation: "
                    f"{artifact.training_corpus_limitation}"
                )
                for reference in artifact.legal_references:
                    self.stdout.write(
                        f"  legal text={reference.name}; "
                        f"archive=legal/{reference.document.filename}; "
                        f"sha256={reference.document.sha256}; "
                        f"source={reference.source_url}"
                    )
            return
        if not options["accept_licenses"]:
            raise CommandError(
                "Review the manifest with --list, then pass --accept-licenses."
            )

        try:
            results = provision_models(
                settings.MODELS_ROOT,
                settings.WORK_ROOT,
                accept_licenses=True,
                artifacts=selected,
                replace_invalid=options["replace_invalid"],
            )
        except ProvisioningError as error:
            raise CommandError(f"Model provisioning failed ({error.code}).") from error

        for result in results:
            self.stdout.write(f"model {result.artifact.name}: {result.status}")
        self.stdout.write(
            self.style.SUCCESS(f"Verified {len(results)} local model artifact(s).")
        )
