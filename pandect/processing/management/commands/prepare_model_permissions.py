"""Explicitly migrate verified public model files for isolated job readers."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from pandect.processing.provisioning import (
    ProvisioningError,
    prepare_model_permissions,
)


class Command(BaseCommand):
    help = "Share digest-verified public models/licenses with isolated media jobs."

    def handle(self, *args, **options):
        try:
            count = prepare_model_permissions(settings.MODELS_ROOT)
        except ProvisioningError as error:
            raise CommandError(
                f"Model permission preparation failed ({error.code}). "
                "Keep unknown files private and replace invalid model artifacts "
                "only after reviewing their licenses."
            ) from error
        self.stdout.write(f"Prepared {count} verified public model/license file(s).")
