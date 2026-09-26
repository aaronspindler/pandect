"""Container entry point and non-mutating readiness probe."""

import os
import sys


def main():
    if sys.argv[1:] == ["--healthcheck"]:
        if os.geteuid() == 0:
            os.setgroups([])
            os.setgid(int(os.environ["PANDECT_RUNNER_APP_GID"]))
            os.setuid(int(os.environ["PANDECT_RUNNER_APP_UID"]))
        from .client import status

        status()
    elif not sys.argv[1:]:
        from .server import main as serve

        serve()
    else:
        raise SystemExit("Usage: python -m pandect.media_runner [--healthcheck]")


if __name__ == "__main__":
    main()
