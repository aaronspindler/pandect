"""Bounded child-process entry point for evidence-free model inference."""

import argparse
import json
import os
from pathlib import Path

# These settings are applied before importing any native inference bindings.
for variable in (
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "OMP_NUM_THREADS",
    "OMP_THREAD_LIMIT",
    "OPENBLAS_NUM_THREADS",
):
    os.environ[variable] = "1"

from pandect.processing.provisioning import (  # noqa: E402
    MODEL_SMOKE_REPORT_PREFIX,
    smoke_infer_models,
)


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--models-root", required=True, type=Path)
    options = parser.parse_args()
    checks = smoke_infer_models(options.models_root)
    report = {
        "schema_version": "1",
        "checks": [check.as_dict() for check in checks],
    }
    encoded = json.dumps(report, separators=(",", ":"), sort_keys=True).encode()
    os.write(1, MODEL_SMOKE_REPORT_PREFIX + encoded + b"\n")
    return 0


if __name__ == "__main__":  # pragma: no branch - module entry point.
    raise SystemExit(main())
