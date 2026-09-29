import errno
import os
from hashlib import sha256
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from pandect.processing.extractors.errors import ToolTimedOutError
from pandect.processing.extractors.faces import (
    AlignedFace,
    ApparentAttributes,
    HeadPose,
    ImageFrame,
)
from pandect.processing.extractors.voice import VadSegment
from pandect.processing.model_manifest import (
    MODEL_ARTIFACTS,
    LegalDocument,
    LegalReference,
    ModelArtifact,
)
from pandect.processing.provisioning import (
    LEGAL_DIRECTORY_NAME,
    MODEL_SMOKE_OUTPUT_LIMIT_BYTES,
    MODEL_SMOKE_TIMEOUT_SECONDS,
    NOTICE_FILENAME,
    ModelInferenceCheck,
    ModelLoadCheck,
    ProvisionedArtifact,
    ProvisioningError,
    StorageCheck,
    _exercise_face_runtime,
    _exercise_voice_runtime,
    check_models,
    check_work_storage,
    prepare_model_permissions,
    provision_models,
    smoke_infer_models,
    smoke_infer_models_isolated,
    smoke_load_models,
)


def _artifact(
    value=b"model",
    *,
    byte_size=None,
    filename="test-model.onnx",
    note="",
    training_corpus_limitation="Test weights have no production corpus.",
    legal_references=(),
):
    return ModelArtifact(
        name="Test model",
        filename=filename,
        sha256=sha256(value).hexdigest(),
        license="MIT",
        source="https://models.example.test/test-model.onnx",
        revision="test-1",
        byte_size=len(value) if byte_size is None else byte_size,
        note=note,
        training_corpus_limitation=training_corpus_limitation,
        legal_references=legal_references,
    )


def _legal_reference(
    value=b"upstream legal text\n",
    *,
    filename="upstream-license.txt",
    source="https://licenses.example.test/upstream-license.txt",
):
    return LegalReference(
        name="Upstream model license",
        source=source,
        document=LegalDocument(
            filename=filename,
            sha256=sha256(value).hexdigest(),
            byte_size=len(value),
        ),
    )


def test_manifest_is_digest_qualified_and_contains_both_openvino_pairs():
    assert len({artifact.filename for artifact in MODEL_ARTIFACTS}) == len(
        MODEL_ARTIFACTS
    )
    assert all(len(artifact.sha256) == 64 for artifact in MODEL_ARTIFACTS)
    assert all(artifact.source.startswith("https://") for artifact in MODEL_ARTIFACTS)
    assert all(artifact.training_corpus_limitation for artifact in MODEL_ARTIFACTS)
    assert all(artifact.legal_references for artifact in MODEL_ARTIFACTS)
    assert all(
        reference.source_url.startswith("https://")
        and len(reference.document.sha256) == 64
        and reference.document.byte_size > 0
        for artifact in MODEL_ARTIFACTS
        for reference in artifact.legal_references
    )
    values = {artifact.filename: artifact for artifact in MODEL_ARTIFACTS}
    assert values["age-gender-recognition-retail-0013.xml"].sha256 == (
        "5452aa9372d38f92e54af7f9a2b23e61f5a3bcb90ed17bc90b50111784dd574b"
    )
    assert values["age-gender-recognition-retail-0013.bin"].sha256 == (
        "6f36b6ec9020bf26151e2ec0e9d4d0848a0694935a681ff54e60c01273e931f3"
    )
    assert values["head-pose-estimation-adas-0001.xml"].sha256 == (
        "bea4b6428168093ac827a544ddf1d96894106811d8c481bc372aa555b0e8450a"
    )
    assert values["head-pose-estimation-adas-0001.bin"].sha256 == (
        "fd7e39c5f115a0fddf192b471a796d55342907a164580d78911c8716a3645056"
    )


@pytest.mark.parametrize(
    "filename,source",
    (("../escape.onnx", "https://example.test/model"), ("model", "http://x.test")),
)
def test_manifest_rejects_unsafe_filename_or_source(filename, source):
    with pytest.raises(ValueError):
        ModelArtifact(
            name="Unsafe",
            filename=filename,
            sha256="a" * 64,
            license="MIT",
            source=source,
            revision="test",
        )


def test_model_checks_require_digest_and_declared_size(tmp_path):
    artifact = _artifact()
    missing = check_models(tmp_path, artifacts=(artifact,))[0]
    assert not missing.present
    assert not missing.valid

    artifact.path(tmp_path).write_bytes(b"model")
    valid = check_models(tmp_path, artifacts=(artifact,))[0]
    assert valid.valid
    assert valid.actual_byte_size == 5

    wrong_size = _artifact(byte_size=6)
    assert not check_models(tmp_path, artifacts=(wrong_size,))[0].valid


def test_model_permissions_only_share_verified_public_files(tmp_path):
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    root = data / "models"
    root.mkdir(mode=0o700)
    legal_root = root / LEGAL_DIRECTORY_NAME
    legal_root.mkdir(mode=0o700)
    reference = _legal_reference()
    artifact = _artifact(legal_references=(reference,))
    model = artifact.path(root)
    model.write_bytes(b"model")
    model.chmod(0o600)
    license_path = legal_root / reference.document.filename
    license_path.write_bytes(b"upstream legal text\n")
    license_path.chmod(0o600)
    notice = root / NOTICE_FILENAME
    notice.write_text("private operator annotation")
    notice.chmod(0o600)
    unknown = root / "private.bin"
    unknown.write_bytes(b"private")
    unknown.chmod(0o600)
    before = {
        path: path.read_bytes() for path in (model, license_path, notice, unknown)
    }

    assert prepare_model_permissions(root, artifacts=(artifact,)) == 2
    assert prepare_model_permissions(root, artifacts=(artifact,)) == 2
    assert data.stat().st_mode & 0o777 == 0o700
    assert root.stat().st_mode & 0o777 == 0o755
    assert legal_root.stat().st_mode & 0o777 == 0o755
    assert model.stat().st_mode & 0o777 == 0o644
    assert license_path.stat().st_mode & 0o777 == 0o644
    assert notice.stat().st_mode & 0o777 == 0o600
    assert unknown.stat().st_mode & 0o777 == 0o600
    assert before == {path: path.read_bytes() for path in before}


@pytest.mark.parametrize("kind", ("unknown", "invalid", "symlink", "hardlink"))
def test_model_permission_migration_rejects_unverified_public_entries(tmp_path, kind):
    root = tmp_path / "models"
    root.mkdir(mode=0o700)
    artifact = _artifact()
    target = artifact.path(root)
    if kind == "unknown":
        target = root / "not-in-manifest.bin"
        target.write_bytes(b"model")
    elif kind == "invalid":
        target.write_bytes(b"corrupt")
    else:
        external = tmp_path / "sensitive"
        external.write_bytes(b"model")
        if kind == "symlink":
            target.symlink_to(external)
        else:
            os.link(external, target)
    target.chmod(0o644)
    with pytest.raises(ProvisioningError):
        prepare_model_permissions(root, artifacts=(artifact,))
    assert root.stat().st_mode & 0o777 == 0o700


def test_private_invalid_model_is_not_made_public(tmp_path):
    artifact = _artifact()
    model = artifact.path(tmp_path)
    model.write_bytes(b"invalid")
    model.chmod(0o600)
    assert prepare_model_permissions(tmp_path, artifacts=(artifact,)) == 0
    assert model.stat().st_mode & 0o777 == 0o600


def test_permission_migration_does_not_follow_model_root_symlink(tmp_path):
    external = tmp_path / "external"
    external.mkdir(mode=0o700)
    root = tmp_path / "models"
    root.symlink_to(external, target_is_directory=True)
    with pytest.raises(ProvisioningError, match="provisioning failed"):
        prepare_model_permissions(root, artifacts=(_artifact(),))
    assert external.stat().st_mode & 0o777 == 0o700


def test_provisioning_refuses_symlink_roots_before_any_download(tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    root = tmp_path / "models"
    root.symlink_to(external, target_is_directory=True)
    with pytest.raises(ProvisioningError) as failure:
        provision_models(
            root,
            tmp_path / "work",
            artifacts=(_artifact(),),
            accept_licenses=True,
            downloader=lambda *_args: pytest.fail("no download permitted"),
        )
    assert failure.value.code == "model_storage_contains_symlink"
    assert not list(external.iterdir())


def test_permission_command_reports_only_counts_and_preserves_private_data(
    settings,
    tmp_path,
):
    settings.MODELS_ROOT = tmp_path / "models"
    settings.MODELS_ROOT.mkdir(mode=0o700)
    output = StringIO()
    call_command("prepare_model_permissions", stdout=output)
    assert output.getvalue() == "Prepared 0 verified public model/license file(s).\n"


def test_model_check_rejects_symlink_even_with_matching_digest(tmp_path):
    artifact = _artifact()
    external = tmp_path / "other"
    external.write_bytes(b"model")
    artifact.path(tmp_path).symlink_to(external)
    assert not check_models(tmp_path, artifacts=(artifact,))[0].valid


def test_provisioning_requires_explicit_license_acceptance(tmp_path):
    with pytest.raises(ProvisioningError) as failure:
        provision_models(
            tmp_path / "models",
            tmp_path / "work",
            accept_licenses=False,
            artifacts=(_artifact(),),
            downloader=lambda _artifact, destination: destination.write(b"model"),
        )
    assert failure.value.code == "license_acceptance_required"


def test_provisioning_verifies_fsyncs_and_atomically_installs_model(tmp_path):
    root = tmp_path / "models"
    work = tmp_path / "work"
    artifact = _artifact()

    results = provision_models(
        root,
        work,
        accept_licenses=True,
        artifacts=(artifact,),
        downloader=lambda _artifact, destination: (
            destination.write(b"mo"),
            destination.write(b"del"),
        ),
    )

    assert results == (ProvisionedArtifact(artifact, "provisioned"),)
    assert artifact.path(root).read_bytes() == b"model"
    assert check_models(root, artifacts=(artifact,))[0].valid
    assert not list(work.iterdir())
    notice = (root / NOTICE_FILENAME).read_text()
    assert "license: MIT" in notice
    assert artifact.sha256 in notice
    assert "models.example.test" in notice


def test_provisioning_archives_exact_hash_bound_upstream_legal_text(tmp_path):
    root = tmp_path / "models"
    work = tmp_path / "work"
    legal_bytes = b"exact upstream license without a final newline"
    reference = _legal_reference(legal_bytes)
    artifact = _artifact(legal_references=(reference,))
    legal_downloads = []

    result = provision_models(
        root,
        work,
        accept_licenses=True,
        artifacts=(artifact,),
        downloader=lambda _artifact, destination: destination.write(b"model"),
        legal_downloader=lambda actual, destination: (
            legal_downloads.append(actual),
            destination.write(legal_bytes),
        ),
    )

    assert result == (ProvisionedArtifact(artifact, "provisioned"),)
    assert legal_downloads == [reference]
    archived = root / LEGAL_DIRECTORY_NAME / reference.document.filename
    assert archived.read_bytes() == legal_bytes
    assert sha256(archived.read_bytes()).hexdigest() == reference.document.sha256
    notice = (root / NOTICE_FILENAME).read_text()
    assert f"archived file: legal/{reference.document.filename}" in notice
    assert f"SHA-256: {reference.document.sha256}" in notice
    assert reference.source_url in notice
    assert not list(work.iterdir())


def test_legal_archive_is_cumulative_and_never_redownloads_or_replaces(tmp_path):
    root = tmp_path / "models"
    work = tmp_path / "work"
    first_bytes = b"first upstream license\n"
    second_bytes = b"second upstream notice\n"
    first_reference = _legal_reference(first_bytes, filename="first-license.txt")
    second_reference = _legal_reference(
        second_bytes,
        filename="second-notice.txt",
        source="https://licenses.example.test/second-notice.txt",
    )
    payloads = {
        first_reference.document.filename: first_bytes,
        second_reference.document.filename: second_bytes,
    }

    def legal_downloader(reference, destination):
        destination.write(payloads[reference.document.filename])

    provision_models(
        root,
        work,
        accept_licenses=True,
        artifacts=(
            _artifact(
                b"first",
                filename="first.onnx",
                legal_references=(first_reference,),
            ),
        ),
        downloader=lambda _artifact, destination: destination.write(b"first"),
        legal_downloader=legal_downloader,
    )
    first_path = root / LEGAL_DIRECTORY_NAME / first_reference.document.filename
    first_before = first_path.read_bytes()

    provision_models(
        root,
        work,
        accept_licenses=True,
        artifacts=(
            _artifact(
                b"first",
                filename="first.onnx",
                legal_references=(first_reference,),
            ),
            _artifact(
                b"second",
                filename="second.onnx",
                legal_references=(first_reference, second_reference),
            ),
        ),
        downloader=lambda _artifact, destination: destination.write(b"second"),
        legal_downloader=legal_downloader,
    )

    assert first_path.read_bytes() == first_before
    assert (
        root / LEGAL_DIRECTORY_NAME / second_reference.document.filename
    ).read_bytes() == second_bytes
    assert (root / NOTICE_FILENAME).read_text().count(
        "--- Pandect model provisioning record ---"
    ) == 2


def test_invalid_existing_legal_document_is_preserved_and_blocks_model(tmp_path):
    root = tmp_path / "models"
    work = tmp_path / "work"
    reference = _legal_reference()
    legal_root = root / LEGAL_DIRECTORY_NAME
    legal_root.mkdir(parents=True)
    target = legal_root / reference.document.filename
    target.write_bytes(b"operator content")
    model_downloaded = False

    def model_downloader(_artifact, destination):
        nonlocal model_downloaded
        model_downloaded = True
        destination.write(b"model")

    with pytest.raises(ProvisioningError) as failure:
        provision_models(
            root,
            work,
            accept_licenses=True,
            artifacts=(_artifact(legal_references=(reference,)),),
            downloader=model_downloader,
            legal_downloader=lambda _reference, destination: destination.write(
                b"upstream legal text\n"
            ),
            replace_invalid=True,
        )

    assert failure.value.code == "existing_legal_document_invalid"
    assert target.read_bytes() == b"operator content"
    assert not model_downloaded
    assert not list(work.iterdir())


def test_bad_legal_download_never_installs_model_and_cleans_work_file(tmp_path):
    root = tmp_path / "models"
    work = tmp_path / "work"
    reference = _legal_reference()

    with pytest.raises(ProvisioningError) as failure:
        provision_models(
            root,
            work,
            accept_licenses=True,
            artifacts=(_artifact(legal_references=(reference,)),),
            downloader=lambda _artifact, destination: destination.write(b"model"),
            legal_downloader=lambda _reference, destination: destination.write(
                b"wrong"
            ),
        )

    assert failure.value.code == "legal_download_checksum_mismatch"
    assert not (root / "test-model.onnx").exists()
    assert not (root / LEGAL_DIRECTORY_NAME / reference.document.filename).exists()
    assert not list(work.iterdir())


def test_partial_provisioning_appends_notice_without_destroying_prior_content(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "models"
    work = tmp_path / "work"
    first = _artifact(
        b"first",
        filename="first.onnx",
        note="First model note.",
        training_corpus_limitation="First corpus limitation.",
    )
    second = _artifact(b"second", filename="second.onnx")
    monkeypatch.setattr(
        "pandect.processing.provisioning.MODEL_ARTIFACTS", (first, second)
    )

    provision_models(
        root,
        work,
        accept_licenses=True,
        artifacts=(first,),
        downloader=lambda _artifact, destination: destination.write(b"first"),
    )
    notice_path = root / NOTICE_FILENAME
    prior_notice = notice_path.read_text()
    operator_annotation = "Operator annotation retained verbatim."
    notice_path.write_text(f"{prior_notice}{operator_annotation}")

    provision_models(
        root,
        work,
        accept_licenses=True,
        artifacts=(second,),
        downloader=lambda _artifact, destination: destination.write(b"second"),
    )

    notice = notice_path.read_text()
    assert notice.startswith(prior_notice)
    assert operator_annotation in notice
    assert notice.index("file: first.onnx") < notice.index("file: second.onnx")
    assert "note: First model note." in notice
    assert "training-corpus limitation: First corpus limitation." in notice
    assert notice.count("--- Pandect model provisioning record ---") == 2
    assert not list(work.iterdir())


def test_failed_notice_replacement_preserves_existing_notice(tmp_path, monkeypatch):
    root = tmp_path / "models"
    work = tmp_path / "work"
    first = _artifact(b"first", filename="first.onnx")
    second = _artifact(b"second", filename="second.onnx")
    provision_models(
        root,
        work,
        accept_licenses=True,
        artifacts=(first,),
        downloader=lambda _artifact, destination: destination.write(b"first"),
    )
    notice_path = root / NOTICE_FILENAME
    original_notice = notice_path.read_bytes()
    real_replace = __import__("os").replace

    def fail_notice_replace(source, destination):
        if Path(destination) == notice_path:
            raise OSError(errno.EIO, "simulated notice replacement failure")
        real_replace(source, destination)

    monkeypatch.setattr(
        "pandect.processing.provisioning.os.replace", fail_notice_replace
    )
    with pytest.raises(ProvisioningError) as failure:
        provision_models(
            root,
            work,
            accept_licenses=True,
            artifacts=(second,),
            downloader=lambda _artifact, destination: destination.write(b"second"),
        )

    assert failure.value.code == "notice_write_failed"
    assert notice_path.read_bytes() == original_notice
    assert not list(work.iterdir())


def test_provisioning_preserves_invalid_existing_file_without_replace_flag(tmp_path):
    root = tmp_path / "models"
    work = tmp_path / "work"
    root.mkdir()
    target = root / "test-model.onnx"
    target.write_bytes(b"operator-file")
    called = False

    def downloader(_artifact, destination):
        nonlocal called
        called = True
        destination.write(b"model")

    with pytest.raises(ProvisioningError) as failure:
        provision_models(
            root,
            work,
            accept_licenses=True,
            artifacts=(_artifact(),),
            downloader=downloader,
        )
    assert failure.value.code == "existing_model_invalid"
    assert target.read_bytes() == b"operator-file"
    assert not called


def test_provisioning_can_explicitly_replace_invalid_file_and_skips_valid(tmp_path):
    root = tmp_path / "models"
    work = tmp_path / "work"
    root.mkdir()
    artifact = _artifact()
    artifact.path(root).write_bytes(b"bad")
    downloads = 0

    def downloader(_artifact, destination):
        nonlocal downloads
        downloads += 1
        destination.write(b"model")

    first = provision_models(
        root,
        work,
        accept_licenses=True,
        artifacts=(artifact,),
        downloader=downloader,
        replace_invalid=True,
        write_notice=False,
    )
    second = provision_models(
        root,
        work,
        accept_licenses=True,
        artifacts=(artifact,),
        downloader=downloader,
        write_notice=False,
    )
    assert first[0].status == "replaced"
    assert second[0].status == "already_valid"
    assert downloads == 1


def test_bad_download_never_replaces_target_and_cleans_work_file(tmp_path):
    root = tmp_path / "models"
    work = tmp_path / "work"
    with pytest.raises(ProvisioningError) as failure:
        provision_models(
            root,
            work,
            accept_licenses=True,
            artifacts=(_artifact(),),
            downloader=lambda _artifact, destination: destination.write(b"wrong"),
            write_notice=False,
        )
    assert failure.value.code == "download_checksum_mismatch"
    assert not (root / "test-model.onnx").exists()
    assert not list(work.iterdir())


def test_work_storage_check_exercises_both_atomic_renames_and_cleans_up(tmp_path):
    work = tmp_path / "work"
    models = tmp_path / "models"
    work.mkdir()
    models.mkdir()

    check = check_work_storage(work, models)

    assert check.valid
    assert not list(work.iterdir())
    assert not list(models.iterdir())


def test_work_storage_check_reports_cross_directory_rename_failure(
    tmp_path, monkeypatch
):
    work = tmp_path / "work"
    models = tmp_path / "models"
    work.mkdir()
    models.mkdir()
    real_replace = __import__("os").replace

    def failing_replace(source, destination):
        if Path(destination).parent == models:
            raise OSError(errno.EXDEV, "different filesystem")
        real_replace(source, destination)

    monkeypatch.setattr("pandect.processing.provisioning.os.replace", failing_replace)
    check = check_work_storage(work, models)

    assert not check.valid
    assert check.error_code == "work_and_model_storage_must_share_a_filesystem"
    assert not list(work.iterdir())
    assert not list(models.iterdir())


def test_model_smoke_loads_components_only_after_manifest_validation(tmp_path):
    artifact = _artifact()
    artifact.path(tmp_path).write_bytes(b"model")
    checks = check_models(tmp_path, artifacts=(artifact,))
    called = []

    loaded = smoke_load_models(
        tmp_path,
        model_checks=checks,
        loaders={"fake-runtime": lambda root: called.append(root)},
    )

    assert loaded == (ModelLoadCheck("fake-runtime", True),)
    assert called == [tmp_path]

    invalid = check_models(tmp_path, artifacts=(_artifact(byte_size=6),))
    called.clear()
    failed = smoke_load_models(
        tmp_path,
        model_checks=invalid,
        loaders={"fake-runtime": lambda root: called.append(root)},
    )
    assert failed == (ModelLoadCheck("manifest", False, "model_integrity_failed"),)
    assert not called


class _FakeFaceRuntime:
    def __init__(self):
        self.calls = []

    def detect(self, frame):
        self.calls.append(("detect", frame))
        return ()

    def align(self, frame, detection):
        self.calls.append(("align", frame, detection))
        raise AssertionError("an empty synthetic detection set must not be aligned")

    def embedding(self, aligned):
        self.calls.append(("embedding", aligned))
        return (3.0, 4.0)

    def expression_logits(self, aligned):
        self.calls.append(("expression", aligned))
        return tuple(float(value) for value in range(7))

    def apparent_attributes(self, aligned):
        self.calls.append(("attributes", aligned))
        return ApparentAttributes(32.0, (0.25, 0.75))

    def head_pose(self, aligned):
        self.calls.append(("pose", aligned))
        return HeadPose(1.0, 2.0, 3.0)


class _FakeVoiceRuntime:
    def __init__(self):
        self.calls = []

    def speech_segments(self, waveform, sample_rate):
        self.calls.append(("vad", len(waveform), sample_rate))
        return (VadSegment(10, len(waveform) - 10),)

    def embedding(self, waveform, sample_rate):
        self.calls.append(("embedding", len(waveform), sample_rate))
        return (1.0, 2.0, 3.0)


def test_model_smoke_exercises_face_and_voice_runtime_inference():
    face = _FakeFaceRuntime()
    frame = ImageFrame(160, 160, object())
    aligned = AlignedFace(112, 112, 120.0, 10.0, object())
    voice = _FakeVoiceRuntime()
    waveform = tuple(0.1 for _ in range(1600))

    _exercise_face_runtime(face, frame, aligned)
    _exercise_voice_runtime(voice, waveform)

    assert [call[0] for call in face.calls] == [
        "detect",
        "embedding",
        "expression",
        "attributes",
        "pose",
    ]
    assert voice.calls == [("vad", 1600, 16000), ("embedding", 1600, 16000)]


def test_model_inference_smoke_requires_integrity_and_sanitizes_failures(tmp_path):
    artifact = _artifact()
    artifact.path(tmp_path).write_bytes(b"model")
    checks = check_models(tmp_path, artifacts=(artifact,))
    called = []

    def pass_inference(root):
        called.append(root)

    def fail_inference(_root):
        raise RuntimeError("secret path and model details")

    results = smoke_infer_models(
        tmp_path,
        model_checks=checks,
        runners={"pass": pass_inference, "fail": fail_inference},
    )

    assert results == (
        ModelInferenceCheck("pass", True),
        ModelInferenceCheck("fail", False, "model_inference_failed"),
    )
    assert called == [tmp_path]
    assert "secret" not in repr(results)

    invalid = check_models(tmp_path, artifacts=(_artifact(byte_size=6),))
    assert smoke_infer_models(
        tmp_path,
        model_checks=invalid,
        runners={"pass": pass_inference},
    ) == (ModelInferenceCheck("manifest", False, "model_integrity_failed"),)
    assert called == [tmp_path]


@pytest.mark.parametrize("transport_metadata", ({}, {"outputs": {}}))
def test_model_inference_smoke_child_is_bounded_and_parses_sanitized_report(
    tmp_path, transport_metadata
):
    calls = []
    checks = (
        ModelInferenceCheck("face/OpenVINO", True),
        ModelInferenceCheck("voice/Silero/CAM++", False, "tool_failed"),
    )
    payload = {
        "schema_version": "1",
        "checks": [check.as_dict() for check in checks],
        **transport_metadata,
    }

    def runner(operation, **options):
        calls.append((operation, options))
        return payload

    assert (
        smoke_infer_models_isolated(
            tmp_path,
            runner=runner,
        )
        == checks
    )
    operation, options = calls[0]
    assert operation == "model_smoke"
    assert options == {
        "inputs": {},
        "parameters": {},
        "timeout_seconds": MODEL_SMOKE_TIMEOUT_SECONDS,
    }


@pytest.mark.parametrize("outputs", ({"unexpected": "file"}, [], None))
def test_model_inference_smoke_rejects_artifact_transport_metadata(tmp_path, outputs):
    result = smoke_infer_models_isolated(
        tmp_path,
        runner=lambda *_args, **_kwargs: {
            "schema_version": "1",
            "checks": [
                {
                    "component": "manifest",
                    "passed": False,
                    "error_code": "model_integrity_failed",
                }
            ],
            "outputs": outputs,
        },
    )
    assert result == (
        ModelInferenceCheck("model-smoke process", False, "invalid_tool_output"),
    )


def test_model_inference_smoke_child_rejects_untrusted_output_and_sanitizes_timeout(
    tmp_path,
):
    def untrusted_runner(_argv, **_options):
        return {
            "schema_version": "1",
            "checks": [
                {
                    "component": "/secret/path",
                    "passed": False,
                    "error_code": "leaked detail",
                }
            ],
        }

    assert smoke_infer_models_isolated(tmp_path, runner=untrusted_runner) == (
        ModelInferenceCheck(
            "model-smoke process",
            False,
            "invalid_tool_output",
        ),
    )

    def timeout_runner(_argv, **_options):
        raise ToolTimedOutError

    assert smoke_infer_models_isolated(tmp_path, runner=timeout_runner) == (
        ModelInferenceCheck("model-smoke process", False, "tool_timed_out"),
    )


def test_model_inference_smoke_rejects_oversized_report(tmp_path):
    assert smoke_infer_models_isolated(
        tmp_path,
        runner=lambda *_args, **_kwargs: {
            "padding": "x" * MODEL_SMOKE_OUTPUT_LIMIT_BYTES
        },
    ) == (ModelInferenceCheck("model-smoke process", False, "invalid_tool_output"),)


@pytest.mark.parametrize("component", ("manifest", "face/OpenVINO"))
def test_model_inference_smoke_rejects_incomplete_success_report(tmp_path, component):
    assert smoke_infer_models_isolated(
        tmp_path,
        runner=lambda *_args, **_kwargs: {
            "schema_version": "1",
            "checks": [{"component": component, "passed": True, "error_code": ""}],
        },
    ) == (ModelInferenceCheck("model-smoke process", False, "invalid_tool_output"),)


def test_model_inference_smoke_reports_runner_failure_without_local_fallback(tmp_path):
    from pandect.media_runner.protocol import RunnerUnavailable

    def unavailable(*_args, **_kwargs):
        raise RunnerUnavailable("private configuration should not be echoed")

    assert smoke_infer_models_isolated(tmp_path, runner=unavailable) == (
        ModelInferenceCheck("model-smoke process", False, "media_runner_failed"),
    )


def test_provision_command_lists_manifest_and_requires_acceptance():
    output = StringIO()
    call_command("provision_processing_models", "--list", stdout=output)
    value = output.getvalue()
    assert "age-gender-recognition-retail-0013.xml" in value
    assert "license=Apache-2.0" in value
    assert "https://storage.openvinotoolkit.org/" in value
    assert "training-corpus limitation:" in value
    assert "legal text=Open Model Zoo Apache-2.0 license" in value
    assert "archive=legal/apache-2.0-c71d239d.txt" in value
    assert "https://raw.githubusercontent.com/openvinotoolkit/" in value

    with pytest.raises(CommandError, match="accept-licenses"):
        call_command("provision_processing_models")


def test_provision_command_passes_explicit_acceptance(settings, tmp_path):
    settings.MODELS_ROOT = tmp_path / "models"
    settings.WORK_ROOT = tmp_path / "work"
    artifact = MODEL_ARTIFACTS[0]
    result = (ProvisionedArtifact(artifact, "already_valid"),)
    output = StringIO()
    with patch(
        "pandect.processing.management.commands.provision_processing_models."
        "provision_models",
        return_value=result,
    ) as provision:
        call_command(
            "provision_processing_models",
            "--accept-licenses",
            "--artifact",
            artifact.filename,
            stdout=output,
        )
    assert provision.call_args.kwargs["accept_licenses"] is True
    assert provision.call_args.kwargs["artifacts"] == (artifact,)
    assert "Verified 1 local model artifact" in output.getvalue()


def test_diagnostics_reports_storage_and_runtime_inference(settings, tmp_path):
    settings.MODELS_ROOT = tmp_path / "models"
    settings.WORK_ROOT = tmp_path / "work"
    artifact = _artifact()
    artifact.path(tmp_path).write_bytes(b"model")
    check = check_models(tmp_path, artifacts=(artifact,))[0]
    output = StringIO()
    command = "pandect.processing.management.commands.processing_diagnostics"
    with (
        patch(f"{command}.media_runner_status", return_value={"busy": False}),
        patch(f"{command}._tool_version", return_value="tool 1.2.3"),
        patch(f"{command}.check_models", return_value=(check,)),
        patch(
            f"{command}.check_work_storage",
            return_value=StorageCheck(True, True, True, True),
        ),
        patch(
            f"{command}.smoke_infer_models_isolated",
            return_value=(ModelInferenceCheck("fake", True),),
        ),
    ):
        call_command("processing_diagnostics", "--smoke-models", stdout=output)

    value = output.getvalue()
    assert "media runner: ready" in value
    assert "tool ffmpeg: available (tool 1.2.3)" in value
    assert "work storage: writable and atomic" in value
    assert "model runtime fake: inference passed" in value


@pytest.mark.parametrize("runner_state", ("busy", "unavailable"))
@pytest.mark.parametrize("smoke_models", (False, True))
def test_diagnostics_reports_runner_state_without_waiting_or_running_jobs(
    runner_state,
    smoke_models,
):
    from pandect.media_runner.protocol import RunnerUnavailable

    output = StringIO()
    command = "pandect.processing.management.commands.processing_diagnostics"
    with (
        patch(f"{command}.media_runner_status") as status,
        patch(f"{command}._tool_version") as tool_version,
        patch(f"{command}.smoke_infer_models_isolated") as model_smoke,
        patch(f"{command}.check_models", return_value=()),
        patch(
            f"{command}.check_work_storage",
            return_value=StorageCheck(True, True, True, True),
        ),
    ):
        if runner_state == "busy":
            status.return_value = {"busy": True}
        else:
            status.side_effect = RunnerUnavailable("private socket configuration")
        if smoke_models:
            with pytest.raises(CommandError, match="capabilities are unavailable"):
                call_command("processing_diagnostics", "--smoke-models", stdout=output)
        else:
            call_command("processing_diagnostics", stdout=output)

    status.assert_called_once_with()
    tool_version.assert_not_called()
    model_smoke.assert_not_called()
    value = output.getvalue()
    assert f"media runner: {runner_state}" in value
    assert f"tool ffmpeg: not checked (media runner {runner_state})" in value
    assert "tool ffmpeg: unavailable" not in value
    assert "Cloudflare AI: disabled" in value
    assert "work storage: writable and atomic" in value
    assert "private socket configuration" not in value
    if runner_state == "busy":
        assert "retry diagnostics after the current job finishes" in value
    else:
        assert "make up" in value
        assert "docker compose logs media-runner" in value


@pytest.mark.parametrize("runner_state", ("busy", "unavailable"))
def test_diagnostics_reports_runner_race_and_does_not_retry_version_jobs(runner_state):
    from pandect.media_runner.protocol import RunnerBusy, RunnerUnavailable

    output = StringIO()
    command = "pandect.processing.management.commands.processing_diagnostics"
    exception = RunnerBusy if runner_state == "busy" else RunnerUnavailable
    with (
        patch(f"{command}.media_runner_status", return_value={"busy": False}),
        patch(
            f"{command}._tool_version",
            side_effect=exception("private error content"),
        ) as tool_version,
        patch(f"{command}.check_models", return_value=()),
        patch(
            f"{command}.check_work_storage",
            return_value=StorageCheck(True, True, True, True),
        ),
    ):
        call_command("processing_diagnostics", stdout=output)
    tool_version.assert_called_once_with("ffmpeg")
    value = output.getvalue()
    assert f"media runner: {runner_state}" in value
    assert "private error content" not in value
    assert f"tool tesseract: not checked (media runner {runner_state})" in value
