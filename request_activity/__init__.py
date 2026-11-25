"""Optional request telemetry for installation operators."""

import os


def capture_enabled():
    return os.environ.get("PANDECT_REQUEST_ACTIVITY_ENABLED", "").lower() in {
        "1",
        "true",
    }
