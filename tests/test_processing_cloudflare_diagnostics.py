import json
from io import StringIO
from unittest.mock import patch

import httpx
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from pandect.processing.cloudflare_diagnostics import (
    CloudflareDiagnosticError,
    CloudflareDiagnosticReport,
    diagnose_cloudflare,
)
from pandect.processing.remote.cloudflare import CloudflareConfig


def _config(**overrides):
    values = {
        "enabled": True,
        "account_id": "account",
        "api_token": "secret-token",
        "gateway_id": "pandect",
        "text_model": "@cf/meta/llama-3.2-11b-vision-instruct",
        "vision_model": "@cf/meta/llama-3.2-11b-vision-instruct",
        "transcription_model": "@cf/deepgram/nova-3",
        "timeout_seconds": 300,
    }
    values.update(overrides)
    return CloudflareConfig(**values)


def _response(request):
    if request.url.path.endswith("/ai/models/search"):
        requested = request.url.params["search"]
        return httpx.Response(
            200,
            json={"success": True, "result": [{"name": requested}]},
        )
    if request.url.path.endswith("/ai/run"):
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {"state": "Completed", "result": {"response": "ready"}},
            },
        )
    raise AssertionError(f"unexpected request path {request.url.path}")


def test_cloudflare_diagnostic_checks_models_and_gateway_without_accepting_license():
    requests = []

    def handler(request):
        requests.append(request)
        return _response(request)

    report = diagnose_cloudflare(
        _config(),
        transport=httpx.MockTransport(handler),
    )

    assert report.credentials_valid
    assert report.gateway_reachable
    assert report.models_available == (
        "@cf/meta/llama-3.2-11b-vision-instruct",
        "@cf/deepgram/nova-3",
    )
    assert report.meta_license_action == "unchanged"
    assert len(requests) == 3
    gateway = requests[-1]
    assert gateway.url.path.endswith("/ai/run")
    assert gateway.headers["cf-aig-gateway-id"] == "pandect"
    assert gateway.headers["cf-aig-collect-log-payload"] == "false"
    assert gateway.headers["cf-aig-skip-cache"] == "true"
    assert gateway.headers["cf-aig-max-attempts"] == "1"
    assert b"evidence" not in gateway.content
    assert b"secret-token" not in gateway.content


def test_meta_license_request_is_impossible_without_explicit_opt_in():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return _response(request)

    diagnose_cloudflare(_config(), transport=httpx.MockTransport(handler))

    assert not any(path.endswith("llama-3.2-11b-vision-instruct") for path in paths)


def test_explicit_meta_license_opt_in_precedes_gateway_smoke():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("llama-3.2-11b-vision-instruct"):
            assert request.method == "POST"
            assert json.loads(request.content) == {"prompt": "agree", "max_tokens": 1}
            return httpx.Response(200, json={"success": True, "result": {}})
        return _response(request)

    report = diagnose_cloudflare(
        _config(),
        accept_meta_license=True,
        transport=httpx.MockTransport(handler),
    )

    assert report.meta_license_action == "accepted_or_reaffirmed"
    assert requests[-2].url.path.endswith("llama-3.2-11b-vision-instruct")
    assert requests[-1].url.path.endswith("/ai/run")


def test_remote_diagnostic_failure_is_sanitized():
    def handler(request):
        return httpx.Response(403, text="secret-token provider private response")

    with pytest.raises(CloudflareDiagnosticError) as failure:
        diagnose_cloudflare(_config(), transport=httpx.MockTransport(handler))

    assert failure.value.code == "credentials_or_model_search_failed"
    assert "secret-token" not in str(failure.value)
    assert "private" not in str(failure.value)


def test_remote_diagnostic_never_trusts_ambient_proxy_credentials():
    original_client = httpx.Client
    with patch("httpx.Client", wraps=original_client) as client:
        diagnose_cloudflare(_config(), transport=httpx.MockTransport(_response))
    assert client.call_args.kwargs["trust_env"] is False


def test_remote_diagnostic_requires_execution_credentials():
    with (
        patch("httpx.Client", side_effect=AssertionError("must not connect")),
        pytest.raises(CloudflareDiagnosticError) as failure,
    ):
        diagnose_cloudflare(_config(api_token=""))
    assert failure.value.code == "configuration_invalid"


def test_remote_diagnostic_rejects_unavailable_configured_model():
    def handler(request):
        return httpx.Response(
            200,
            json={"success": True, "result": [{"name": "different"}]},
        )

    with pytest.raises(CloudflareDiagnosticError) as failure:
        diagnose_cloudflare(_config(), transport=httpx.MockTransport(handler))
    assert failure.value.code == "configured_model_unavailable"


def test_command_requires_cloudflare_flag_for_meta_acceptance():
    with pytest.raises(CommandError, match="requires --cloudflare"):
        call_command("processing_diagnostics", "--accept-meta-license")


def test_command_runs_explicit_cloudflare_diagnostic(settings, tmp_path):
    settings.MODELS_ROOT = tmp_path / "models"
    settings.WORK_ROOT = tmp_path / "work"
    settings.MODELS_ROOT.mkdir()
    settings.WORK_ROOT.mkdir()
    output = StringIO()
    report = CloudflareDiagnosticReport(
        credentials_valid=True,
        models_available=("@cf/model",),
        gateway_reachable=True,
        meta_license_action="accepted_or_reaffirmed",
    )
    command = "pandect.processing.management.commands.processing_diagnostics"
    with (
        patch(f"{command}.media_runner_status", return_value={"busy": False}),
        patch(f"{command}._tool_version", return_value="tool 1"),
        patch(f"{command}.check_models", return_value=()),
        patch(f"{command}.diagnose_cloudflare", return_value=report) as diagnose,
    ):
        call_command(
            "processing_diagnostics",
            "--cloudflare",
            "--accept-meta-license",
            stdout=output,
        )

    assert diagnose.call_args.kwargs["accept_meta_license"] is True
    assert "Cloudflare credentials: valid" in output.getvalue()
    assert "Cloudflare Gateway: reachable" in output.getvalue()
    assert "Cloudflare model @cf/model: available" in output.getvalue()
    assert "Meta license action: accepted_or_reaffirmed" in output.getvalue()


@pytest.mark.django_db
def test_health_and_readiness_do_not_run_remote_diagnostics(client):
    command = "pandect.processing.cloudflare_diagnostics.diagnose_cloudflare"
    with (
        patch(command, side_effect=AssertionError("must remain independent")) as run,
        patch("pandect.core.views.media_runner_status", return_value={"busy": False}),
    ):
        assert client.get("/healthz/").status_code == 200
        assert client.get("/readyz/").status_code == 200
    run.assert_not_called()
