"""Fixed executable entered only after the supervisor drops all job privileges."""

import json
import sys
from pathlib import Path

from pandect.media_runner.operations import run_child_request
from pandect.processing.extractors.errors import ExtractorError


def main() -> int:
    request = json.loads(Path(sys.argv[1]).read_bytes())
    try:
        result = run_child_request(request)
    except ExtractorError as error:
        result = {"error_code": error.code}
    except Exception:
        result = {"error_code": "tool_failed"}
    encoded = json.dumps(result, allow_nan=False, separators=(",", ":")).encode()
    if len(encoded) > 16 * 1024 * 1024:
        encoded = b'{"error_code":"tool_output_limit"}'
    with Path(request["result_path"]).open("xb") as output:
        output.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
