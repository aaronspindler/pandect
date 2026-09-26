"""Explicit, offline-at-runtime provisioning for pinned local models."""

import errno
import hashlib
import json
import math
import os
import re
import stat
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

import httpx

from pandect.media_runner.protocol import RunnerError
from pandect.media_runner.routing import require_child
from pandect.media_runner.validation import validate_operation_result
from pandect.processing.extractors.errors import (
    ExtractorError,
    InvalidExtractorOutputError,
)
from pandect.processing.model_manifest import (
    MODEL_ARTIFACTS,
    LegalReference,
    ModelArtifact,
)

MAX_MODEL_BYTES = 512 * 1024 * 1024
MAX_LEGAL_DOCUMENT_BYTES = 1024 * 1024
LEGAL_DIRECTORY_NAME = "legal"
MODEL_SMOKE_TIMEOUT_SECONDS = 120
MODEL_SMOKE_OUTPUT_LIMIT_BYTES = 64 * 1024
MODEL_SMOKE_REPORT_PREFIX = b"PANDECT_MODEL_SMOKE="
_MODEL_SMOKE_COMPONENTS = {
    "manifest",
    "face/OpenVINO",
    "voice/Silero/CAM++",
}
_ERROR_CODE_PATTERN = re.compile(r"[a-z0-9_]{1,64}")
NOTICE_FILENAME = "PROCESSING-MODELS-NOTICE.txt"


class ProvisioningError(Exception):
    """Sanitized provisioning failure that never includes a URL or local path."""

    def __init__(self, code: str):
        self.code = code
        super().__init__("Local model provisioning failed.")


@dataclass(frozen=True)
class ModelCheck:
    artifact: ModelArtifact
    path: Path
    present: bool
    valid: bool
    actual_sha256: str = ""
    actual_byte_size: int = 0


@dataclass(frozen=True)
class ProvisionedArtifact:
    artifact: ModelArtifact
    status: str


@dataclass(frozen=True)
class StorageCheck:
    writable: bool
    file_fsync: bool
    atomic_rename: bool
    cross_directory_atomic_rename: bool
    error_code: str = ""

    @property
    def valid(self) -> bool:
        return all(
            (
                self.writable,
                self.file_fsync,
                self.atomic_rename,
                self.cross_directory_atomic_rename,
            )
        )


@dataclass(frozen=True)
class ModelLoadCheck:
    component: str
    loaded: bool
    error_code: str = ""


@dataclass(frozen=True)
class ModelInferenceCheck:
    component: str
    passed: bool
    error_code: str = ""

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "component": self.component,
            "passed": self.passed,
            "error_code": self.error_code,
        }


class _HashingWriter:
    def __init__(self, destination: BinaryIO, maximum_bytes: int):
        self.destination = destination
        self.maximum_bytes = maximum_bytes
        self.digest = hashlib.sha256()
        self.byte_size = 0

    def write(self, value: bytes) -> int:
        if not isinstance(value, bytes):
            raise TypeError("provisioning downloader must write bytes")
        if self.byte_size + len(value) > self.maximum_bytes:
            raise ProvisioningError("download_too_large")
        written = self.destination.write(value)
        if written != len(value):
            raise ProvisioningError("short_write")
        self.digest.update(value)
        self.byte_size += written
        return written


ModelDownloader = Callable[[ModelArtifact, BinaryIO], None]
LegalDownloader = Callable[[LegalReference, BinaryIO], None]
ModelLoader = Callable[[Path], None]
ModelInferenceRunner = Callable[[Path], None]


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_models(
    root: Path,
    *,
    artifacts: Iterable[ModelArtifact] = MODEL_ARTIFACTS,
) -> tuple[ModelCheck, ...]:
    checks = []
    for artifact in artifacts:
        path = artifact.path(root)
        present = not root.is_symlink() and not path.is_symlink() and path.is_file()
        try:
            byte_size = path.stat().st_size if present else 0
            actual = (
                sha256_path(path) if present and byte_size <= MAX_MODEL_BYTES else ""
            )
        except OSError:
            present = False
            byte_size = 0
            actual = ""
        expected_size_matches = (
            artifact.byte_size is None or byte_size == artifact.byte_size
        )
        valid = (
            present and actual == artifact.sha256.casefold() and expected_size_matches
        )
        checks.append(ModelCheck(artifact, path, present, valid, actual, byte_size))
    return tuple(checks)


def prepare_model_permissions(
    root: Path,
    *,
    artifacts: Iterable[ModelArtifact] = MODEL_ARTIFACTS,
) -> int:
    """Explicitly share only verified public artifacts with isolated job UIDs.

    This does not traverse the surrounding private data directory, recursively
    chmod unknown content, or expose the provisioning notice (which may contain
    operator annotations). Open descriptors bind validation and chmod to the
    same regular, singly-linked file; symlinks are never followed.
    """
    selected = tuple(artifacts)
    if not root.exists() and not root.is_symlink():
        return 0
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    directories = []
    verified = []
    try:
        root_fd = os.open(root, directory_flags)
        directories.append(root_fd)
        public_files = {
            artifact.filename: (artifact.sha256, artifact.byte_size, MAX_MODEL_BYTES)
            for artifact in selected
        }
        public_legal = {
            reference.document.filename: (
                reference.document.sha256,
                reference.document.byte_size,
                MAX_LEGAL_DOCUMENT_BYTES,
            )
            for reference in _legal_references(selected)
        }
        inventories = [(root_fd, public_files)]
        try:
            legal_fd = os.open(LEGAL_DIRECTORY_NAME, directory_flags, dir_fd=root_fd)
        except FileNotFoundError:
            pass
        else:
            directories.append(legal_fd)
            inventories.append((legal_fd, public_legal))

        # Refuse an already-public unknown/invalid entry before making the
        # enclosing directory traversable. Private legacy notices are retained.
        for directory_fd, inventory in inventories:
            for name in os.listdir(directory_fd):
                if directory_fd == root_fd and name == LEGAL_DIRECTORY_NAME:
                    continue
                entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if stat.S_ISLNK(entry.st_mode):
                    raise ProvisioningError("model_storage_contains_symlink")
                expected = inventory.get(name)
                valid = False
                if expected and stat.S_ISREG(entry.st_mode) and entry.st_nlink == 1:
                    descriptor = os.open(name, file_flags, dir_fd=directory_fd)
                    try:
                        actual = os.fstat(descriptor)
                        digest, size, maximum = expected
                        if (
                            stat.S_ISREG(actual.st_mode)
                            and actual.st_nlink == 1
                            and actual.st_size <= maximum
                            and (size is None or actual.st_size == size)
                        ):
                            with os.fdopen(os.dup(descriptor), "rb") as source:
                                actual_digest = hashlib.file_digest(source, "sha256")
                            valid = actual_digest.hexdigest() == digest.casefold()
                        if valid:
                            verified.append(descriptor)
                            descriptor = None
                    finally:
                        if descriptor is not None:
                            os.close(descriptor)
                if not valid:
                    if entry.st_mode & 0o007:
                        raise ProvisioningError("unverified_model_storage_is_public")
                    # A private unknown entry must not be owned by a future
                    # isolated job identity, which could read its owner bits.
                    if entry.st_uid not in {0, os.geteuid()}:
                        raise ProvisioningError("unverified_model_storage_owner")

        for descriptor in verified:
            os.fchmod(descriptor, 0o644)
        for descriptor in directories:
            os.fchmod(descriptor, 0o755)
        return len(verified)
    except ProvisioningError:
        raise
    except OSError as error:
        raise ProvisioningError("model_permissions_unavailable") from error
    finally:
        for descriptor in (*verified, *directories):
            os.close(descriptor)


def _http_download(
    artifact: ModelArtifact | LegalReference,
    destination: BinaryIO,
) -> None:
    timeout = httpx.Timeout(300.0, connect=10.0)
    with httpx.Client(
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": "pandect-model-provisioner/1"},
    ) as client:
        with client.stream("GET", artifact.source) as response:
            response.raise_for_status()
            if response.url.scheme != "https":
                raise ProvisioningError("insecure_redirect")
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    declared_size = int(content_length)
                except ValueError as error:
                    raise ProvisioningError("invalid_content_length") from error
                if declared_size > MAX_MODEL_BYTES:
                    raise ProvisioningError("download_too_large")
            for chunk in response.iter_bytes(1024 * 1024):
                destination.write(chunk)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _legal_references(
    artifacts: Iterable[ModelArtifact],
) -> tuple[LegalReference, ...]:
    """Return one transfer per content-addressed legal document, in stable order."""
    references: dict[str, LegalReference] = {}
    for artifact in artifacts:
        for reference in artifact.legal_references:
            previous = references.setdefault(reference.document.filename, reference)
            if previous.document != reference.document:
                raise ProvisioningError("legal_manifest_conflict")
    return tuple(references.values())


def _legal_document_valid(path: Path, reference: LegalReference) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        size = path.stat().st_size
        return (
            size == reference.document.byte_size
            and size <= MAX_LEGAL_DOCUMENT_BYTES
            and sha256_path(path) == reference.document.sha256.casefold()
        )
    except OSError:
        return False


def _provision_legal_documents(
    root: Path,
    work_root: Path,
    artifacts: tuple[ModelArtifact, ...],
    *,
    downloader: LegalDownloader,
) -> None:
    """Archive exact upstream legal text without replacing any prior document."""
    references = _legal_references(artifacts)
    if not references:
        return
    legal_root = root / LEGAL_DIRECTORY_NAME
    try:
        if legal_root.is_symlink() or (legal_root.exists() and not legal_root.is_dir()):
            raise ProvisioningError("legal_storage_invalid")
        legal_root.mkdir(mode=0o700, exist_ok=True)
        os.chmod(legal_root, 0o700)
        _fsync_directory(root)
    except ProvisioningError:
        raise
    except OSError as error:
        raise ProvisioningError("legal_storage_unavailable") from error

    for reference in references:
        target = legal_root / reference.document.filename
        if target.is_symlink() or target.exists():
            if _legal_document_valid(target, reference):
                continue
            raise ProvisioningError("existing_legal_document_invalid")

        temporary = work_root / (f".{reference.document.filename}.{uuid4().hex}.part")
        try:
            with temporary.open("xb") as raw_destination:
                destination = _HashingWriter(
                    raw_destination,
                    MAX_LEGAL_DOCUMENT_BYTES,
                )
                try:
                    downloader(reference, destination)
                except ProvisioningError:
                    raise
                except Exception as error:
                    raise ProvisioningError("legal_download_failed") from error
                raw_destination.flush()
                os.fsync(raw_destination.fileno())

            if destination.digest.hexdigest() != reference.document.sha256.casefold():
                raise ProvisioningError("legal_download_checksum_mismatch")
            if destination.byte_size != reference.document.byte_size:
                raise ProvisioningError("legal_download_size_mismatch")

            os.chmod(temporary, 0o600)
            # A hard-link publish is atomic and refuses to replace a document that
            # appeared after the preflight check. Work/model storage is required
            # to share a filesystem by the processing diagnostic.
            os.link(temporary, target, follow_symlinks=False)
            _fsync_directory(legal_root)
            temporary.unlink()
            _fsync_directory(work_root)
        except ProvisioningError:
            raise
        except FileExistsError as error:
            raise ProvisioningError("existing_legal_document_invalid") from error
        except OSError as error:
            raise ProvisioningError("legal_atomic_install_failed") from error
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _notice_text(artifacts: Iterable[ModelArtifact]) -> str:
    lines = [
        "--- Pandect model provisioning record ---",
        f"License acceptance recorded at: {datetime.now(UTC).isoformat()}",
        "Provisioning was requested only after explicit operator acceptance.",
        "",
    ]
    for artifact in artifacts:
        lines.extend(
            (
                artifact.name,
                f"  file: {artifact.filename}",
                f"  revision: {artifact.revision}",
                f"  SHA-256: {artifact.sha256}",
                f"  license: {artifact.license}",
                f"  source: {artifact.source}",
            )
        )
        if artifact.note:
            lines.append(f"  note: {artifact.note}")
        if artifact.training_corpus_limitation:
            lines.append(
                f"  training-corpus limitation: {artifact.training_corpus_limitation}"
            )
        for reference in artifact.legal_references:
            lines.extend(
                (
                    f"  legal text: {reference.name}",
                    f"    archived file: {LEGAL_DIRECTORY_NAME}/"
                    f"{reference.document.filename}",
                    f"    SHA-256: {reference.document.sha256}",
                    f"    source: {reference.source_url}",
                )
            )
            if reference.note:
                lines.append(f"    note: {reference.note}")
        lines.append("")
    return "\n".join(lines)


def _write_notice(
    root: Path,
    work_root: Path,
    artifacts: tuple[ModelArtifact, ...],
) -> None:
    temporary = work_root / f".{NOTICE_FILENAME}.{uuid4().hex}.part"
    target = root / NOTICE_FILENAME
    try:
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ProvisioningError("notice_write_failed")
        with temporary.open("xb") as destination:
            copied_existing = False
            final_existing_byte = b""
            if target.is_file():
                with target.open("rb") as existing:
                    for chunk in iter(lambda: existing.read(1024 * 1024), b""):
                        destination.write(chunk)
                        copied_existing = True
                        final_existing_byte = chunk[-1:]
            else:
                destination.write(
                    b"Pandect local processing model notice\n"
                    b"Earlier provisioning records and operator annotations "
                    b"are retained.\n"
                )
                copied_existing = True
                final_existing_byte = b"\n"

            if copied_existing:
                if final_existing_byte != b"\n":
                    destination.write(b"\n")
                destination.write(b"\n")
            destination.write(_notice_text(artifacts).encode("utf-8"))
            destination.flush()
            os.fsync(destination.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        _fsync_directory(root)
    except OSError as error:
        raise ProvisioningError("notice_write_failed") from error
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def provision_models(
    root: Path,
    work_root: Path,
    *,
    accept_licenses: bool,
    artifacts: Iterable[ModelArtifact] = MODEL_ARTIFACTS,
    downloader: ModelDownloader | None = None,
    legal_downloader: LegalDownloader | None = None,
    replace_invalid: bool = False,
    write_notice: bool = True,
) -> tuple[ProvisionedArtifact, ...]:
    """Download and atomically install only manifest-bound local model files."""
    if not accept_licenses:
        raise ProvisioningError("license_acceptance_required")
    selected = tuple(artifacts)
    if not selected:
        raise ProvisioningError("empty_manifest")
    if root.is_symlink() or work_root.is_symlink():
        raise ProvisioningError("model_storage_contains_symlink")
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        work_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as error:
        raise ProvisioningError("storage_unavailable") from error

    _provision_legal_documents(
        root,
        work_root,
        selected,
        downloader=legal_downloader or _http_download,
    )

    download = downloader or _http_download
    results = []
    for artifact in selected:
        target = artifact.path(root)
        existing = check_models(root, artifacts=(artifact,))[0]
        if existing.valid:
            results.append(ProvisionedArtifact(artifact, "already_valid"))
            continue
        if target.exists() and not replace_invalid:
            raise ProvisioningError("existing_model_invalid")

        temporary = work_root / f".{artifact.filename}.{uuid4().hex}.part"
        try:
            with temporary.open("xb") as raw_destination:
                destination = _HashingWriter(raw_destination, MAX_MODEL_BYTES)
                try:
                    download(artifact, destination)
                except ProvisioningError:
                    raise
                except Exception as error:
                    raise ProvisioningError("download_failed") from error
                raw_destination.flush()
                os.fsync(raw_destination.fileno())

            if destination.digest.hexdigest() != artifact.sha256.casefold():
                raise ProvisioningError("download_checksum_mismatch")
            if (
                artifact.byte_size is not None
                and destination.byte_size != artifact.byte_size
            ):
                raise ProvisioningError("download_size_mismatch")

            os.chmod(temporary, 0o600)
            replaced = target.exists()
            os.replace(temporary, target)
            _fsync_directory(root)
            results.append(
                ProvisionedArtifact(
                    artifact,
                    "replaced" if replaced else "provisioned",
                )
            )
        except ProvisioningError:
            raise
        except OSError as error:
            raise ProvisioningError("atomic_install_failed") from error
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    if write_notice:
        _write_notice(root, work_root, selected)
    # Include the complete manifest so provisioning one artifact does not
    # reject already-provisioned public siblings. Test/custom manifests remain
    # supported without changing any files absent from either allowlist.
    complete = {artifact.filename: artifact for artifact in MODEL_ARTIFACTS}
    complete.update({artifact.filename: artifact for artifact in selected})
    prepare_model_permissions(root, artifacts=complete.values())
    return tuple(results)


def check_work_storage(work_root: Path, model_root: Path) -> StorageCheck:
    """Exercise the exact write, fsync, and rename path provisioning needs."""
    token = uuid4().hex
    written_path = work_root / f".processing-diagnostic-{token}.write"
    renamed_path = work_root / f".processing-diagnostic-{token}.renamed"
    installed_path = model_root / f".processing-diagnostic-{token}.installed"
    marker = b"pandect-processing-storage-check\n"
    writable = False
    file_fsync = False
    atomic_rename = False
    cross_directory = False
    error_code = ""
    try:
        if not work_root.is_dir() or not model_root.is_dir():
            return StorageCheck(False, False, False, False, "directory_unavailable")
        with written_path.open("xb") as destination:
            destination.write(marker)
            writable = True
            destination.flush()
            os.fsync(destination.fileno())
            file_fsync = True
        os.replace(written_path, renamed_path)
        _fsync_directory(work_root)
        if renamed_path.read_bytes() != marker:
            return StorageCheck(True, True, False, False, "rename_validation_failed")
        atomic_rename = True
        os.replace(renamed_path, installed_path)
        _fsync_directory(work_root)
        _fsync_directory(model_root)
        if installed_path.read_bytes() != marker:
            return StorageCheck(True, True, True, False, "install_validation_failed")
        cross_directory = True
    except OSError as error:
        if not writable:
            error_code = "write_failed"
        elif not file_fsync:
            error_code = "fsync_failed"
        elif not atomic_rename:
            error_code = "atomic_rename_failed"
        elif error.errno == errno.EXDEV:
            error_code = "work_and_model_storage_must_share_a_filesystem"
        else:
            error_code = "cross_directory_atomic_rename_failed"
    finally:
        for path in (written_path, renamed_path, installed_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
    return StorageCheck(
        writable,
        file_fsync,
        atomic_rename,
        cross_directory,
        error_code,
    )


def _load_face_models(root: Path) -> None:
    require_child()
    from pandect.processing.extractors.faces import FaceModelPaths, OpenCVFaceRuntime

    OpenCVFaceRuntime(FaceModelPaths.from_root(root))


def _load_voice_models(root: Path) -> None:
    require_child()
    from pandect.processing.extractors.voice import (
        SherpaOnnxVoiceRuntime,
        VoiceModelPaths,
    )

    SherpaOnnxVoiceRuntime(VoiceModelPaths.from_root(root))


def smoke_load_models(
    root: Path,
    *,
    model_checks: Iterable[ModelCheck] | None = None,
    loaders: Mapping[str, ModelLoader] | None = None,
) -> tuple[ModelLoadCheck, ...]:
    """Load and compile provisioned models without processing evidence data."""
    checks = tuple(model_checks) if model_checks is not None else check_models(root)
    if any(check.artifact.required and not check.valid for check in checks):
        return (ModelLoadCheck("manifest", False, "model_integrity_failed"),)
    selected_loaders = loaders or {
        "face/OpenVINO": _load_face_models,
        "voice": _load_voice_models,
    }
    results = []
    for name, loader in selected_loaders.items():
        try:
            loader(root)
        except ExtractorError as error:
            results.append(ModelLoadCheck(name, False, error.code))
        except (ImportError, OSError, RuntimeError, ValueError):
            results.append(ModelLoadCheck(name, False, "model_load_failed"))
        else:
            results.append(ModelLoadCheck(name, True))
    return tuple(results)


def _finite_vector(
    values,
    *,
    exact_length: int | None = None,
    require_nonzero: bool = False,
) -> tuple[float, ...]:
    try:
        result = tuple(float(value) for value in values)
    except (TypeError, ValueError, OverflowError) as error:
        raise InvalidExtractorOutputError from error
    if not result or not all(math.isfinite(value) for value in result):
        raise InvalidExtractorOutputError
    if exact_length is not None and len(result) != exact_length:
        raise InvalidExtractorOutputError
    if require_nonzero and math.sqrt(sum(value * value for value in result)) <= 1e-12:
        raise InvalidExtractorOutputError
    return result


def _exercise_face_runtime(runtime, frame, aligned) -> None:
    """Execute every provisioned face model against bounded synthetic pixels."""
    detections = tuple(runtime.detect(frame))
    if detections:
        # A stylized synthetic face may or may not cross the detector threshold.
        # When it does, also exercise the exact YuNet-to-SFace alignment seam.
        runtime.align(frame, detections[0])
    _finite_vector(runtime.embedding(aligned), require_nonzero=True)
    _finite_vector(runtime.expression_logits(aligned), exact_length=7)
    attributes = runtime.apparent_attributes(aligned)
    if attributes is None or attributes.binary_visual_class_scores is None:
        raise InvalidExtractorOutputError
    _finite_vector(
        (attributes.apparent_age,),
        exact_length=1,
    )
    _finite_vector(attributes.binary_visual_class_scores, exact_length=2)
    pose = runtime.head_pose(aligned)
    if pose is None:
        raise InvalidExtractorOutputError
    _finite_vector((pose.yaw, pose.pitch, pose.roll), exact_length=3)


def _infer_face_models(root: Path) -> None:
    require_child()
    import cv2
    import numpy as np

    from pandect.processing.extractors.faces import (
        AlignedFace,
        FaceModelPaths,
        ImageFrame,
        OpenCVFaceRuntime,
    )

    runtime = OpenCVFaceRuntime(FaceModelPaths.from_root(root))
    pixels = np.full((160, 160, 3), 210, dtype=np.uint8)
    cv2.ellipse(pixels, (80, 82), (46, 58), 0, 0, 360, (170, 185, 200), -1)
    cv2.circle(pixels, (64, 70), 7, (45, 45, 45), -1)
    cv2.circle(pixels, (96, 70), 7, (45, 45, 45), -1)
    cv2.ellipse(pixels, (80, 104), (20, 10), 0, 0, 180, (55, 55, 55), 3)
    aligned_pixels = cv2.resize(pixels, (112, 112))
    grayscale = cv2.cvtColor(aligned_pixels, cv2.COLOR_BGR2GRAY)
    frame = ImageFrame(160, 160, pixels)
    aligned = AlignedFace(
        112,
        112,
        float(grayscale.mean()),
        float(cv2.Laplacian(grayscale, cv2.CV_64F).var()),
        aligned_pixels,
    )
    _exercise_face_runtime(runtime, frame, aligned)


def _synthetic_voice_waveform() -> tuple[float, ...]:
    sample_rate = 16_000
    duration_seconds = 3
    values = []
    for index in range(sample_rate * duration_seconds):
        seconds = index / sample_rate
        envelope = 0.55 + 0.35 * math.sin(2 * math.pi * 3 * seconds)
        voiced = math.sin(2 * math.pi * 125 * seconds)
        voiced += 0.45 * math.sin(2 * math.pi * 250 * seconds)
        voiced += 0.2 * math.sin(2 * math.pi * 375 * seconds)
        values.append(0.12 * envelope * voiced)
    return tuple(values)


def _exercise_voice_runtime(runtime, waveform) -> None:
    """Execute Silero VAD and CAM++ against a bounded synthetic waveform."""
    from pandect.processing.extractors.voice import SAMPLE_RATE

    segments = tuple(runtime.speech_segments(waveform, SAMPLE_RATE))
    for segment in segments:
        if not (
            0 <= int(segment.start_sample) < int(segment.end_sample) <= len(waveform)
        ):
            raise InvalidExtractorOutputError
    _finite_vector(
        runtime.embedding(waveform, SAMPLE_RATE),
        require_nonzero=True,
    )


def _infer_voice_models(root: Path) -> None:
    require_child()
    from pandect.processing.extractors.voice import (
        SherpaOnnxVoiceRuntime,
        VoiceModelPaths,
    )

    runtime = SherpaOnnxVoiceRuntime(VoiceModelPaths.from_root(root))
    _exercise_voice_runtime(runtime, _synthetic_voice_waveform())


def smoke_infer_models(
    root: Path,
    *,
    model_checks: Iterable[ModelCheck] | None = None,
    runners: Mapping[str, ModelInferenceRunner] | None = None,
) -> tuple[ModelInferenceCheck, ...]:
    """Run evidence-free inference only after every required digest is valid."""
    checks = tuple(model_checks) if model_checks is not None else check_models(root)
    if any(check.artifact.required and not check.valid for check in checks):
        return (ModelInferenceCheck("manifest", False, "model_integrity_failed"),)
    selected_runners = runners or {
        "face/OpenVINO": _infer_face_models,
        "voice/Silero/CAM++": _infer_voice_models,
    }
    results = []
    for name, inference in selected_runners.items():
        try:
            inference(root)
        except ExtractorError as error:
            results.append(ModelInferenceCheck(name, False, error.code))
        except Exception:  # noqa: BLE001 - report is deliberately sanitized.
            results.append(ModelInferenceCheck(name, False, "model_inference_failed"))
        else:
            results.append(ModelInferenceCheck(name, True))
    return tuple(results)


def _parse_model_smoke_report(value: bytes) -> tuple[ModelInferenceCheck, ...]:
    report_line = next(
        (
            line[len(MODEL_SMOKE_REPORT_PREFIX) :]
            for line in reversed(value.splitlines())
            if line.startswith(MODEL_SMOKE_REPORT_PREFIX)
        ),
        None,
    )
    if report_line is None:
        raise InvalidExtractorOutputError
    try:
        decoded = json.loads(report_line)
    except (UnicodeDecodeError, TypeError, ValueError) as error:
        raise InvalidExtractorOutputError from error
    if (
        not isinstance(decoded, dict)
        or set(decoded) != {"schema_version", "checks"}
        or decoded.get("schema_version") != "1"
    ):
        raise InvalidExtractorOutputError
    checks = decoded.get("checks")
    if not isinstance(checks, list) or not checks:
        raise InvalidExtractorOutputError
    results = []
    seen_components = set()
    for check in checks:
        if not isinstance(check, dict):
            raise InvalidExtractorOutputError
        component = check.get("component")
        passed = check.get("passed")
        error_code = check.get("error_code", "")
        if (
            not isinstance(component, str)
            or component not in _MODEL_SMOKE_COMPONENTS
            or component in seen_components
            or not isinstance(passed, bool)
            or not isinstance(error_code, str)
            or (passed and error_code)
            or (not passed and not _ERROR_CODE_PATTERN.fullmatch(error_code))
        ):
            raise InvalidExtractorOutputError
        seen_components.add(component)
        results.append(ModelInferenceCheck(component, passed, error_code))
    if seen_components == {"manifest"}:
        if results[0].passed:
            raise InvalidExtractorOutputError
    elif seen_components != {"face/OpenVINO", "voice/Silero/CAM++"}:
        raise InvalidExtractorOutputError
    return tuple(results)


def smoke_infer_models_isolated(
    root: Path,
    *,
    runner=None,
) -> tuple[ModelInferenceCheck, ...]:
    """Run inference only through the confined runner's fixed model mount."""
    del root  # The public runner protocol never accepts arbitrary model paths.
    if runner is None:
        from pandect.media_runner.client import run_operation

        runner = run_operation
    try:
        result = runner(
            "model_smoke",
            inputs={},
            parameters={},
            timeout_seconds=MODEL_SMOKE_TIMEOUT_SECONDS,
        )
        if not isinstance(result, dict):
            raise InvalidExtractorOutputError
        # Transport metadata is not part of the model report. This operation
        # must never return artifacts, even from a compromised parser child.
        report = dict(result)
        if report.pop("outputs", {}) != {}:
            raise InvalidExtractorOutputError
        validate_operation_result("model_smoke", report)
        encoded = json.dumps(report, allow_nan=False).encode("utf-8")
        if len(encoded) > MODEL_SMOKE_OUTPUT_LIMIT_BYTES:
            raise InvalidExtractorOutputError
        return _parse_model_smoke_report(MODEL_SMOKE_REPORT_PREFIX + encoded)
    except ExtractorError as error:
        return (ModelInferenceCheck("model-smoke process", False, error.code),)
    except RunnerError:
        return (
            ModelInferenceCheck("model-smoke process", False, "media_runner_failed"),
        )
    except (TypeError, ValueError):
        return (
            ModelInferenceCheck("model-smoke process", False, "invalid_tool_output"),
        )
