"""Offline-only model validation shared by local inference extractors."""

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from pandect.processing.extractors.errors import (
    ModelIntegrityError,
    ModelUnavailableError,
)


@dataclass(frozen=True)
class LocalModelProvenance:
    """Display-safe provenance for one local model file.

    Local paths are deliberately excluded. Model paths can disclose operator
    details and are irrelevant to reproducibility once the identifier, byte
    size, and digest have been recorded.
    """

    identifier: str
    sha256: str
    byte_size: int
    expected_sha256_verified: bool

    def as_dict(self) -> dict[str, str | int | bool]:
        return {
            "identifier": self.identifier,
            "sha256": self.sha256,
            "byte_size": self.byte_size,
            "expected_sha256_verified": self.expected_sha256_verified,
        }


def validate_local_model(
    path: Path,
    *,
    identifier: str,
    expected_sha256: str,
) -> LocalModelProvenance:
    """Validate an explicitly supplied local model without downloading it."""
    if not path.is_file():
        raise ModelUnavailableError
    expected = expected_sha256.casefold()
    if len(expected) != 64 or any(
        character not in "0123456789abcdef" for character in expected
    ):
        raise ModelIntegrityError

    digest = sha256()
    byte_size = 0
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
                byte_size += len(chunk)
    except OSError as error:
        raise ModelUnavailableError from error
    actual = digest.hexdigest()
    if actual != expected:
        raise ModelIntegrityError
    return LocalModelProvenance(
        identifier=identifier,
        sha256=actual,
        byte_size=byte_size,
        expected_sha256_verified=True,
    )
