"""Small, explicit helpers for Pandect's local environment settings."""

import os
import secrets
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured


def boolean(name: str, *, default: bool) -> bool:
    """Read a strict boolean environment variable."""
    value = os.environ.get(name)
    if value is None:
        return default

    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ImproperlyConfigured(
        f"{name} must be one of: 1, 0, true, false, yes, no, on, off."
    )


def comma_separated(name: str, *, default: tuple[str, ...]) -> list[str]:
    """Read a comma-separated list without accepting empty entries."""
    value = os.environ.get(name)
    if value is None:
        return list(default)

    entries = [entry.strip() for entry in value.split(",") if entry.strip()]
    if not entries:
        raise ImproperlyConfigured(f"{name} must contain at least one value.")
    return entries


def create_data_directory(base_dir: Path) -> Path:
    """Return and create Pandect's durable local data directory."""
    configured = os.environ.get("PANDECT_DATA_DIR")
    if configured is None:
        path = base_dir / "data"
    elif not configured.strip():
        raise ImproperlyConfigured("PANDECT_DATA_DIR must not be empty.")
    else:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = base_dir / path

    path = path.resolve()
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise ImproperlyConfigured(
            f"Unable to create the Pandect data directory at {path}."
        ) from exc
    return path


def _read_secret(path: Path) -> str:
    secret = path.read_text(encoding="utf-8").strip()
    if not secret:
        raise ImproperlyConfigured(
            "The generated Pandect secret file is empty; remove it and retry."
        )
    return secret


def load_secret_key(data_dir: Path) -> str:
    """Load an override or create a restrictive, persistent local secret."""
    configured = os.environ.get("PANDECT_SECRET_KEY")
    if configured is not None and configured != "":
        if not configured.strip():
            raise ImproperlyConfigured(
                "PANDECT_SECRET_KEY must not contain only whitespace."
            )
        return configured

    path = data_dir / "secret.key"
    try:
        return _read_secret(path)
    except FileNotFoundError:
        generated = secrets.token_urlsafe(48)

    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Another local process may have won the creation race.
        return _read_secret(path)

    with os.fdopen(descriptor, "w", encoding="utf-8") as secret_file:
        secret_file.write(f"{generated}\n")
        secret_file.flush()
        os.fsync(secret_file.fileno())
    return generated
