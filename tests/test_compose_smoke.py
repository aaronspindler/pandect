from unittest.mock import Mock

import pytest

from pandect.processing.model_manifest import MODEL_ARTIFACTS
from pandect.processing.provisioning import ModelInferenceCheck
from scripts.compose_smoke import optional_models


def test_optional_model_smoke_requires_explicit_manifest_failure(monkeypatch, tmp_path):
    runner = Mock(
        return_value=(ModelInferenceCheck("manifest", False, "model_integrity_failed"),)
    )
    monkeypatch.setattr(
        "pandect.processing.provisioning.smoke_infer_models_isolated", runner
    )
    optional_models(tmp_path)
    runner.assert_called_once_with(tmp_path / "models")


@pytest.mark.parametrize(
    "result",
    [
        (ModelInferenceCheck("model-smoke process", False, "media_runner_failed"),),
        (ModelInferenceCheck("model-smoke process", False, "invalid_tool_output"),),
        (ModelInferenceCheck("manifest", True),),
    ],
)
def test_optional_model_smoke_rejects_transport_and_schema_failures(
    monkeypatch, tmp_path, result
):
    monkeypatch.setattr(
        "pandect.processing.provisioning.smoke_infer_models_isolated",
        Mock(return_value=result),
    )
    with pytest.raises(ValueError, match="explicit manifest failure"):
        optional_models(tmp_path)


def test_optional_model_smoke_does_not_assume_a_real_model_stack_is_empty(
    monkeypatch, tmp_path
):
    models = tmp_path / "models"
    models.mkdir()
    (models / MODEL_ARTIFACTS[0].filename).write_bytes(b"not actual model weights")
    runner = Mock()
    monkeypatch.setattr(
        "pandect.processing.provisioning.smoke_infer_models_isolated", runner
    )
    with pytest.raises(ValueError, match="clean synthetic stack"):
        optional_models(tmp_path)
    runner.assert_not_called()
