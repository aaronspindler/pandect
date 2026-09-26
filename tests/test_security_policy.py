import importlib.util
import json
from datetime import date
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "security_policy", Path(__file__).parents[1] / "scripts/security_policy.py"
)
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


def exception(**overrides):
    return {
        "scanner": "trivy",
        "advisory": "CVE-2026-12345",
        "owner": "Maintainer",
        "justification": "The affected feature is absent; tracked upstream fix.",
        "expires": "2026-10-01",
        **overrides,
    }


@pytest.mark.parametrize(
    "entries",
    [
        {},
        [exception(expires="2026-09-24")],
        [exception(expires="invalid")],
        [exception(advisory="*")],
        [exception(owner="")],
        [exception(scanner="other")],
        [exception(extra="field")],
        [exception(), exception()],
    ],
)
def test_exceptions_fail_closed(tmp_path, entries):
    path = tmp_path / "exceptions.json"
    path.write_text(json.dumps(entries))
    with pytest.raises(ValueError):
        policy.load_exceptions(path, today=date(2026, 9, 24))


def test_valid_exact_time_bound_exceptions(tmp_path):
    path = tmp_path / "exceptions.json"
    path.write_text(json.dumps([exception()]))
    assert policy.load_exceptions(path, today=date(2026, 9, 24)) == {
        "uv": set(),
        "trivy": {"CVE-2026-12345"},
    }


def test_image_policy_blocks_fixable_high_and_critical():
    findings = [
        {
            "VulnerabilityID": f"CVE-{index}",
            "PkgName": "example",
            "InstalledVersion": "1.0",
            "FixedVersion": fixed,
            "Severity": severity,
        }
        for index, (severity, fixed) in enumerate(
            [("HIGH", "2.0"), ("CRITICAL", "2.0"), ("MEDIUM", "2.0"), ("HIGH", "")]
        )
    ]
    report = image_report(findings)
    assert len(policy.unexcepted_vulnerabilities(report, set())) == 2
    assert len(policy.unexcepted_vulnerabilities(report, {"CVE-0"})) == 1
    assert not policy.unexcepted_vulnerabilities(report, {"CVE-0", "CVE-1"})


def image_report(findings):
    return {
        "SchemaVersion": 2,
        "Metadata": {"OS": {"Family": "debian"}},
        "Results": [
            {
                "Class": "os-pkgs",
                "Type": "debian",
                "Packages": [
                    {"Name": name}
                    for name in ("ffmpeg", "libseccomp2", "tesseract-ocr")
                ],
                "Vulnerabilities": findings,
            },
            {
                "Class": "lang-pkgs",
                "Type": "python-pkg",
                "Packages": [
                    {"Name": name} for name in ("Django", "gunicorn", "pillow")
                ],
            },
        ],
    }


@pytest.mark.parametrize(
    "report",
    [
        {},
        {"SchemaVersion": 1, "Results": []},
        {"SchemaVersion": 2, "Results": []},
        {"SchemaVersion": 2, "Results": [{}]},
    ],
)
def test_unknown_image_report_fails_closed(report):
    with pytest.raises(ValueError):
        policy.unexcepted_vulnerabilities(report, set())


@pytest.mark.parametrize("missing_class", ["os-pkgs", "lang-pkgs"])
def test_incomplete_image_scan_fails_closed(missing_class):
    report = image_report([])
    report["Results"] = [
        target for target in report["Results"] if target["Class"] != missing_class
    ]
    with pytest.raises(ValueError):
        policy.unexcepted_vulnerabilities(report, set())


def test_maintenance_image_requires_its_own_package_coverage():
    report = image_report([])
    report["Results"][0]["Packages"] = [
        {"Name": name} for name in ("libc6", "libpcre2-8-0")
    ]
    report["Results"][1]["Packages"] = [{"Name": "pip"}]
    assert not policy.unexcepted_vulnerabilities(
        report, set(), image_kind="maintenance"
    )
    with pytest.raises(ValueError):
        policy.unexcepted_vulnerabilities(report, set(), image_kind="runtime")
    report["Results"].pop()
    with pytest.raises(ValueError):
        policy.unexcepted_vulnerabilities(report, set(), image_kind="maintenance")
