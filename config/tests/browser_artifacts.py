"""Keep synthetic browser captures inside the ignored validation directory."""

import os
from pathlib import Path


def artifact_directory(subdirectory):
    root = Path(__file__).resolve().parents[2] / "data/validation"
    root = root.resolve()
    path = os.path.realpath(
        os.environ.get("PANDECT_BROWSER_ARTIFACTS", root / subdirectory)
    )
    if not path.startswith(str(root) + os.sep):
        raise ValueError("PANDECT_BROWSER_ARTIFACTS must stay inside data/validation.")
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    return directory
