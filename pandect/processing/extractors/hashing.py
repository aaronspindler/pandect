"""Streaming fingerprints for canonical evidence and derived artifacts."""

from dataclasses import dataclass
from hashlib import sha256
from os import PathLike
from typing import BinaryIO

DEFAULT_HASH_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class FileFingerprint:
    """A stable content fingerprint without filesystem metadata."""

    sha256: str
    size_bytes: int

    def as_dict(self) -> dict[str, str | int]:
        return {"sha256": self.sha256, "size_bytes": self.size_bytes}


def sha256_bytes(value: bytes) -> str:
    """Return the lowercase SHA-256 hex digest of ``value``."""
    return sha256(value).hexdigest()


def fingerprint_stream(
    stream: BinaryIO,
    *,
    chunk_size: int = DEFAULT_HASH_CHUNK_BYTES,
    preserve_position: bool = True,
) -> FileFingerprint:
    """Hash a seekable binary stream and optionally restore its position."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    original_position = stream.tell() if preserve_position else None
    digest = sha256()
    size_bytes = 0
    try:
        stream.seek(0)
        while chunk := stream.read(chunk_size):
            if not isinstance(chunk, bytes):
                raise TypeError("fingerprint_stream requires a binary stream")
            digest.update(chunk)
            size_bytes += len(chunk)
    finally:
        if original_position is not None:
            stream.seek(original_position)

    return FileFingerprint(digest.hexdigest(), size_bytes)


def fingerprint_file(
    path: str | PathLike[str],
    *,
    chunk_size: int = DEFAULT_HASH_CHUNK_BYTES,
) -> FileFingerprint:
    """Hash all bytes in ``path`` without loading the file into memory."""
    with open(path, "rb") as stream:
        return fingerprint_stream(
            stream,
            chunk_size=chunk_size,
            preserve_position=False,
        )
