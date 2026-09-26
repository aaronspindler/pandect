"""Fail-closed, expiring advisory exceptions for explicit network security checks."""

import argparse
import json
import re
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{1,127}\Z")
FIELDS = {"scanner", "advisory", "owner", "justification", "expires"}


def load_exceptions(path: Path, *, today: date | None = None) -> dict[str, set[str]]:
    today = today or datetime.now(UTC).date()
    entries = json.loads(path.read_text())
    if not isinstance(entries, list):
        raise ValueError("Security exceptions must be a JSON array.")
    result: dict[str, set[str]] = {"uv": set(), "trivy": set()}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != FIELDS:
            raise ValueError(
                "Every exception needs exactly the five documented fields."
            )
        if not all(
            isinstance(value, str) and value.strip() for value in entry.values()
        ):
            raise ValueError("Security exception fields must be nonempty strings.")
        scanner, advisory = entry["scanner"], entry["advisory"]
        if scanner not in result or not ID_PATTERN.fullmatch(advisory):
            raise ValueError("Unknown scanner or invalid advisory ID; no wildcards.")
        if advisory in result[scanner]:
            raise ValueError("Duplicate security exception.")
        expiry = date.fromisoformat(entry["expires"])
        if expiry <= today:
            raise ValueError(f"Expired security exception: {scanner}/{advisory}.")
        result[scanner].add(advisory)
    return result


def unexcepted_vulnerabilities(
    report: dict, exceptions: set[str], *, image_kind: str = "runtime"
) -> list[str]:
    if report.get("SchemaVersion") != 2 or not isinstance(report.get("Results"), list):
        raise ValueError("Unexpected Trivy report schema; refusing to pass this scan.")
    if report.get("Metadata", {}).get("OS", {}).get("Family") != "debian":
        raise ValueError("Trivy did not identify the expected Debian runtime image.")
    os_packages = set()
    python_packages = set()
    for target in report["Results"]:
        packages = {
            package["Name"].lower().replace("_", "-")
            for package in target.get("Packages", [])
        }
        if target.get("Class") == "os-pkgs" and target.get("Type") == "debian":
            os_packages.update(packages)
        if target.get("Class") == "lang-pkgs" and target.get("Type") == "python-pkg":
            python_packages.update(packages)
    required_os = {
        "runtime": {"ffmpeg", "libseccomp2", "tesseract-ocr"},
        "maintenance": {"libc6", "libpcre2-8-0"},
    }[image_kind]
    required_python = {
        "runtime": {"django", "gunicorn", "pillow"},
        "maintenance": {"pip"},
    }[image_kind]
    if not required_os <= os_packages:
        raise ValueError(f"Trivy did not scan the expected {image_kind} OS packages.")
    if not required_python <= python_packages:
        raise ValueError(
            f"Trivy did not scan the expected {image_kind} Python packages."
        )
    blocked = []
    for target in report["Results"]:
        for finding in target.get("Vulnerabilities") or []:
            if finding["Severity"] not in {"HIGH", "CRITICAL"}:
                continue
            if not finding.get("FixedVersion"):
                continue
            advisory = finding["VulnerabilityID"]
            if advisory not in exceptions:
                blocked.append(
                    f"{advisory}: {finding['PkgName']} {finding['InstalledVersion']} "
                    f"(fixed: {finding['FixedVersion']})"
                )
    return sorted(set(blocked))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("validate", "uv", "trivy"))
    parser.add_argument("--exceptions", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--image-kind", choices=("runtime", "maintenance"), default="runtime"
    )
    args = parser.parse_args()
    try:
        exceptions = load_exceptions(args.exceptions)
        if args.mode == "uv":
            command = ["uv", "audit", "--locked", "--no-python-downloads"]
            for advisory in sorted(exceptions["uv"]):
                command.extend(("--ignore", advisory))
            return subprocess.run(command, check=False).returncode
        if args.mode == "trivy":
            if args.report is None:
                raise ValueError("Trivy policy requires --report.")
            blocked = unexcepted_vulnerabilities(
                json.loads(args.report.read_text()),
                exceptions["trivy"],
                image_kind=args.image_kind,
            )
            for finding in blocked:
                print(finding)
            if blocked:
                print(
                    "Image audit failed: fix applicable vulnerabilities before release."
                )
                return 1
            print("No unexcepted fixable HIGH/CRITICAL image vulnerabilities.")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"Security policy validation failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
