"""Fail-closed capacity checks for the operator's local evidence volume."""

import shutil
from pathlib import Path

from django.conf import settings


class StorageCapacityError(ValueError):
    """A safe, actionable message that never includes a private storage path."""


def require_storage_space(path, additional_bytes: int = 0) -> None:
    """Require room for the next write and the reserve, on the target volume.

    This is a preflight guard, not a filesystem quota: concurrent writers and
    device failures can still exhaust a volume. Callers must also roll back
    their own provisional files when a write fails.
    """
    if additional_bytes < 0:
        raise ValueError("The required storage size cannot be negative.")
    target = Path(path)
    while not target.exists() and target.parent != target:
        target = target.parent
    try:
        free_bytes = shutil.disk_usage(target).free
    except OSError as error:
        raise StorageCapacityError(
            "The data volume's available space could not be checked. "
            "Check its availability and permissions, then retry."
        ) from error
    reserve = settings.STORAGE_FREE_RESERVE_BYTES
    if free_bytes < reserve + additional_bytes:
        raise StorageCapacityError(
            "The data volume is too full to safely store this upload or result. "
            "Free space or move Pandect to a larger volume, then retry. "
            f"At least {reserve:,} bytes must remain free."
        )
