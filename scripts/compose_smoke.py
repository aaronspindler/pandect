"""Synthetic-only assertions used inside the disposable Compose smoke stack."""

import argparse
import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path

SNAPSHOT_NAME = ".compose-smoke-hashes.json"


def hashes(data: Path) -> dict[str, str]:
    result = {}
    for path in sorted(data.rglob("*")):
        if path.is_symlink():
            raise ValueError("Unexpected symlink in synthetic smoke data.")
        if path.is_file() and path.name != SNAPSHOT_NAME:
            with path.open("rb") as stream:
                result[path.relative_to(data).as_posix()] = hashlib.file_digest(
                    stream, "sha256"
                ).hexdigest()
    return result


def seed(data: Path) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import django

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "pandect.config.settings")
    django.setup()
    from pandect.catalog.services import create_catalogued_item
    from pandect.notes.models import Note

    create_catalogued_item(
        Note,
        item_fields={"title": "Synthetic Compose recovery check"},
        extension_fields={"body": "Synthetic fixture, not investigation evidence."},
    )
    for name, content in {
        "media/smoke-original.bin": b"Synthetic original\x00\xff",
        "derived/smoke-derived.txt": b"Synthetic derived artifact",
        "models/smoke-license.txt": b"Synthetic model notice; not a real model.",
    }.items():
        path = data / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def optional_models(data: Path) -> None:
    """Check absent optional weights through actual IPC, never download them."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from pandect.processing.model_manifest import MODEL_ARTIFACTS
    from pandect.processing.provisioning import (
        ModelInferenceCheck,
        smoke_infer_models_isolated,
    )

    models = data / "models"
    if any((models / artifact.filename).exists() for artifact in MODEL_ARTIFACTS):
        raise ValueError("The optional-model smoke requires a clean synthetic stack.")
    checks = smoke_infer_models_isolated(models)
    if checks != (ModelInferenceCheck("manifest", False, "model_integrity_failed"),):
        raise ValueError(
            "Absent optional models did not return the explicit manifest failure."
        )
    print("Absent optional models report manifest/model_integrity_failed through IPC.")


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode", choices=("health", "seed", "snapshot", "verify", "optional-models")
    )
    args = parser.parse_args()
    data = Path(os.environ["PANDECT_DATA_DIR"])
    if args.mode == "health":
        for path in ("/", "/healthz/", "/readyz/"):
            with urllib.request.urlopen(
                f"http://127.0.0.1:8000{path}", timeout=5
            ) as response:
                if response.status != 200:
                    raise ValueError(f"Smoke HTTP request failed: {path}.")
    elif args.mode == "seed":
        seed(data)
    elif args.mode == "optional-models":
        optional_models(data)
    elif args.mode == "snapshot":
        (data / SNAPSHOT_NAME).write_text(json.dumps(hashes(data), sort_keys=True))
    else:
        expected = json.loads((data / SNAPSHOT_NAME).read_text())
        actual = hashes(data)
        if not expected or actual != expected:
            # This helper runs only against its own synthetic fixture. Report
            # names, never file contents or secret hashes, for actionable CI.
            for name in sorted(set(expected) | set(actual)):
                if expected.get(name) != actual.get(name):
                    state = (
                        "added"
                        if name not in expected
                        else "missing"
                        if name not in actual
                        else "changed"
                    )
                    print(f"Synthetic recovery mismatch ({state}): {name}")
            raise ValueError("Restored data hashes differ from the stopped source.")
        print("Recovery preserved all synthetic files, databases, and the secret.")


if __name__ == "__main__":
    main()
