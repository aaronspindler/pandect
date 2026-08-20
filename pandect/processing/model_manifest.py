"""Pinned local model inventory used by diagnostics and processors.

Every entry is an immutable download plan: provisioning accepts only these
HTTPS sources and commits a file only after its complete SHA-256 digest has
been verified.  Processing code never downloads model weights.
"""

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


def _validate_digest(value: str, *, label: str) -> None:
    digest = value.casefold()
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ValueError(f"{label} SHA-256 must contain 64 hexadecimal characters")


def _validate_https_url(value: str, *, label: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"{label} source must be an absolute HTTPS URL")


@dataclass(frozen=True)
class LegalDocument:
    """Exact, source-controlled upstream legal text archived with model files."""

    filename: str
    sha256: str
    byte_size: int

    def __post_init__(self) -> None:
        if Path(self.filename).name != self.filename or not self.filename:
            raise ValueError("legal document filename must be a single path component")
        _validate_digest(self.sha256, label="legal document")
        if self.byte_size <= 0:
            raise ValueError("legal document byte size must be positive")


@dataclass(frozen=True)
class LegalReference:
    """Model-specific upstream URL bound to an exact vendored legal document."""

    name: str
    source: str = field(repr=False)
    document: LegalDocument
    note: str = ""

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("legal reference name must not be blank")
        _validate_https_url(self.source, label="legal reference")

    @property
    def source_url(self) -> str:
        return self.source


APACHE_2_C71_DOCUMENT = LegalDocument(
    filename="apache-2.0-c71d239d.txt",
    sha256="c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4",
    byte_size=11_357,
)
APACHE_2_CFC_DOCUMENT = LegalDocument(
    filename="apache-2.0-cfc7749b.txt",
    sha256="cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
    byte_size=11_358,
)
YUNET_MIT_DOCUMENT = LegalDocument(
    filename="yunet-mit-c83b8120.txt",
    sha256="c83b8120c50ccbd4c4f96edf53141bdd566ebb8f8e9227e415326aa1b1aba958",
    byte_size=1_085,
)
SILERO_MIT_DOCUMENT = LegalDocument(
    filename="silero-vad-mit-2e63e9a3.txt",
    sha256="2e63e9a38b6e8fc0c7bc37ce174caca1862870856c6daf5697cfb785e925520b",
    byte_size=1_075,
)

OPENCV_YUNET_LICENSE = LegalReference(
    name="YuNet model MIT license",
    source=(
        "https://raw.githubusercontent.com/opencv/opencv_zoo/"
        "47534e27c9851bb1128ccc0102f1145e27f23f98/"
        "models/face_detection_yunet/LICENSE"
    ),
    document=YUNET_MIT_DOCUMENT,
)
OPENCV_SFACE_LICENSE = LegalReference(
    name="SFace model Apache-2.0 license",
    source=(
        "https://raw.githubusercontent.com/opencv/opencv_zoo/"
        "47534e27c9851bb1128ccc0102f1145e27f23f98/"
        "models/face_recognition_sface/LICENSE"
    ),
    document=APACHE_2_CFC_DOCUMENT,
)
OPENCV_ZOO_LICENSE = LegalReference(
    name="OpenCV Zoo repository Apache-2.0 license",
    source=(
        "https://raw.githubusercontent.com/opencv/opencv_zoo/"
        "47534e27c9851bb1128ccc0102f1145e27f23f98/LICENSE"
    ),
    document=APACHE_2_C71_DOCUMENT,
    note=(
        "The facial-expression directory's README links a model-local LICENSE "
        "that is absent at the pinned revision; this exact pinned repository "
        "license is retained instead."
    ),
)
THREEDSPEAKER_LICENSE = LegalReference(
    name="3D-Speaker upstream Apache-2.0 license",
    source=(
        "https://raw.githubusercontent.com/modelscope/3D-Speaker/"
        "b6ea24f428a178c02b7ef9d6900569050448c8a1/LICENSE"
    ),
    document=APACHE_2_C71_DOCUMENT,
)
SHERPA_ONNX_LICENSE = LegalReference(
    name="sherpa-onnx distributor Apache-2.0 license",
    source=(
        "https://raw.githubusercontent.com/k2-fsa/sherpa-onnx/"
        "142807252687d81b40d6315f23470a1512a00de3/LICENSE"
    ),
    document=APACHE_2_CFC_DOCUMENT,
)
SILERO_VAD_LICENSE = LegalReference(
    name="Silero VAD upstream MIT license",
    source=(
        "https://raw.githubusercontent.com/snakers4/silero-vad/"
        "915dd3d639b8333a52e001af095f87c5b7f1e0ac/LICENSE"
    ),
    document=SILERO_MIT_DOCUMENT,
)
OPEN_MODEL_ZOO_LICENSE = LegalReference(
    name="Open Model Zoo Apache-2.0 license",
    source=(
        "https://raw.githubusercontent.com/openvinotoolkit/open_model_zoo/"
        "e37a163a2385d9ddaa8e3da1ccb50c80d4fa0802/LICENSE"
    ),
    document=APACHE_2_C71_DOCUMENT,
)


@dataclass(frozen=True)
class ModelArtifact:
    """One explicitly provisioned, hash-verified inference artifact."""

    name: str
    filename: str
    sha256: str
    license: str
    source: str = field(repr=False)
    revision: str
    byte_size: int | None = None
    required: bool = True
    note: str = ""
    training_corpus_limitation: str = ""
    legal_references: tuple[LegalReference, ...] = ()

    def __post_init__(self) -> None:
        if Path(self.filename).name != self.filename or not self.filename:
            raise ValueError("model filename must be a single path component")
        _validate_digest(self.sha256, label="model")
        _validate_https_url(self.source, label="model")
        if self.byte_size is not None and self.byte_size <= 0:
            raise ValueError("model byte size must be positive")
        if not self.training_corpus_limitation.strip():
            raise ValueError("model training-corpus limitation must not be blank")

    @property
    def source_url(self) -> str:
        """Return the transfer URL without exposing it through object repr."""
        return self.source

    def path(self, root: Path) -> Path:
        return root / self.filename


MODEL_ARTIFACTS = (
    ModelArtifact(
        name="YuNet face detector",
        filename="face_detection_yunet_2023mar.onnx",
        sha256="8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
        license="MIT",
        source=(
            "https://github.com/opencv/opencv_zoo/raw/47534e27c9851bb1128ccc0102f1145e27f23f98/"
            "models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
        ),
        revision="opencv-zoo@47534e27 / 2023-03",
        byte_size=232_589,
        training_corpus_limitation=(
            "Upstream reports WIDER Face validation and links a training "
            "repository, but the pinned weight has no complete training-sample "
            "manifest; source licensing, demographic coverage, and exact "
            "preprocessing cannot be independently established."
        ),
        legal_references=(OPENCV_YUNET_LICENSE,),
    ),
    ModelArtifact(
        name="SFace recognizer",
        filename="face_recognition_sface_2021dec.onnx",
        sha256="0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
        license="Apache-2.0",
        source=(
            "https://github.com/opencv/opencv_zoo/raw/47534e27c9851bb1128ccc0102f1145e27f23f98/"
            "models/face_recognition_sface/face_recognition_sface_2021dec.onnx"
        ),
        revision="opencv-zoo@47534e27 / 2021-12",
        byte_size=38_696_353,
        training_corpus_limitation=(
            "Upstream identifies MobileFaceNet with SFace loss and links the "
            "paper and original code, but does not bind this weight to a complete "
            "training-corpus manifest; identity consent, demographic coverage, "
            "and exact preprocessing remain undisclosed."
        ),
        legal_references=(OPENCV_SFACE_LICENSE,),
    ),
    ModelArtifact(
        name="Apparent facial expression model",
        filename="facial_expression_recognition_mobilefacenet_2022july.onnx",
        sha256="4f61307602fc089ce20488a31d4e4614e3c9753a7d6c41578c854858b183e1a9",
        license="Apache-2.0",
        source=(
            "https://github.com/opencv/opencv_zoo/raw/47534e27c9851bb1128ccc0102f1145e27f23f98/"
            "models/facial_expression_recognition/"
            "facial_expression_recognition_mobilefacenet_2022july.onnx"
        ),
        revision="opencv-zoo@47534e27 / 2022-07",
        byte_size=4_791_892,
        training_corpus_limitation=(
            "The pinned OpenCV Zoo page references the model paper but does not "
            "bind the weight to a complete training-corpus or label-provenance "
            "manifest; demographic coverage and expression-label construction "
            "remain undisclosed."
        ),
        legal_references=(OPENCV_ZOO_LICENSE,),
    ),
    ModelArtifact(
        name="CAM++ bilingual speaker embedding model",
        filename="3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx",
        sha256="aa3cfc16963a10586a9393f5035d6d6b57e98d358b347f80c2a30bf4f00ceba2",
        license="Apache-2.0",
        source=(
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
            "speaker-recongition-models/"
            "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
        ),
        revision="sherpa-onnx speaker-recongition-models release",
        byte_size=28_281_164,
        training_corpus_limitation=(
            "The release filename identifies a bilingual zh/en 3D-Speaker "
            "CAM++ export, but the release has no artifact-bound model card or "
            "complete utterance/speaker corpus manifest; consent, licensing, "
            "language balance, and preprocessing remain undisclosed."
        ),
        legal_references=(THREEDSPEAKER_LICENSE, SHERPA_ONNX_LICENSE),
    ),
    ModelArtifact(
        name="Silero VAD v4 (k2-fsa export)",
        filename="silero_vad_v4_9e2449e1.onnx",
        sha256="9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6",
        license="MIT",
        source=(
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
            "asr-models/silero_vad.onnx"
        ),
        revision="sherpa-onnx asr-models release / Silero VAD v4",
        byte_size=643_854,
        training_corpus_limitation=(
            "The artifact is identified as a sherpa-onnx export of Silero VAD "
            "v4, but neither the release nor pinned upstream license supplies a "
            "complete auditable training-corpus manifest; language, acoustic-"
            "domain, consent, and data-license coverage remain undisclosed."
        ),
        legal_references=(SILERO_VAD_LICENSE, SHERPA_ONNX_LICENSE),
    ),
    ModelArtifact(
        name="OpenVINO apparent age/binary visual class FP16 graph",
        filename="age-gender-recognition-retail-0013.xml",
        sha256="5452aa9372d38f92e54af7f9a2b23e61f5a3bcb90ed17bc90b50111784dd574b",
        license="Apache-2.0",
        source=(
            "https://storage.openvinotoolkit.org/repositories/open_model_zoo/"
            "2021.4/models_bin/1/age-gender-recognition-retail-0013/FP16/"
            "age-gender-recognition-retail-0013.xml"
        ),
        revision="Open Model Zoo 2021.4 / IR v10 / FP16",
        byte_size=30_327,
        note=(
            "Paired with age-gender-recognition-retail-0013.bin. Pandect exposes "
            "only broad apparent-age bands and neutral binary visual class scores."
        ),
        training_corpus_limitation=(
            "The pinned Open Model Zoo metadata states the classification task "
            "but provides no complete image-source, annotation, consent, or "
            "demographic training-corpus manifest; population coverage and label "
            "construction therefore cannot be established."
        ),
        legal_references=(OPEN_MODEL_ZOO_LICENSE,),
    ),
    ModelArtifact(
        name="OpenVINO apparent age/binary visual class FP16 weights",
        filename="age-gender-recognition-retail-0013.bin",
        sha256="6f36b6ec9020bf26151e2ec0e9d4d0848a0694935a681ff54e60c01273e931f3",
        license="Apache-2.0",
        source=(
            "https://storage.openvinotoolkit.org/repositories/open_model_zoo/"
            "2021.4/models_bin/1/age-gender-recognition-retail-0013/FP16/"
            "age-gender-recognition-retail-0013.bin"
        ),
        revision="Open Model Zoo 2021.4 / IR v10 / FP16",
        byte_size=4_276_038,
        note="Paired with age-gender-recognition-retail-0013.xml.",
        training_corpus_limitation=(
            "The pinned Open Model Zoo metadata states the classification task "
            "but provides no complete image-source, annotation, consent, or "
            "demographic training-corpus manifest; population coverage and label "
            "construction therefore cannot be established."
        ),
        legal_references=(OPEN_MODEL_ZOO_LICENSE,),
    ),
    ModelArtifact(
        name="OpenVINO head pose FP16 graph",
        filename="head-pose-estimation-adas-0001.xml",
        sha256="bea4b6428168093ac827a544ddf1d96894106811d8c481bc372aa555b0e8450a",
        license="Apache-2.0",
        source=(
            "https://storage.openvinotoolkit.org/repositories/open_model_zoo/"
            "2021.4/models_bin/1/head-pose-estimation-adas-0001/FP16/"
            "head-pose-estimation-adas-0001.xml"
        ),
        revision="Open Model Zoo 2021.4 / IR v10 / FP16",
        byte_size=57_268,
        note="Paired with head-pose-estimation-adas-0001.bin.",
        training_corpus_limitation=(
            "The pinned Open Model Zoo metadata says the network was trained from "
            "scratch for yaw, pitch, roll, and landmarks, but does not disclose a "
            "complete image-source, annotation, consent, or population manifest."
        ),
        legal_references=(OPEN_MODEL_ZOO_LICENSE,),
    ),
    ModelArtifact(
        name="OpenVINO head pose FP16 weights",
        filename="head-pose-estimation-adas-0001.bin",
        sha256="fd7e39c5f115a0fddf192b471a796d55342907a164580d78911c8716a3645056",
        license="Apache-2.0",
        source=(
            "https://storage.openvinotoolkit.org/repositories/open_model_zoo/"
            "2021.4/models_bin/1/head-pose-estimation-adas-0001/FP16/"
            "head-pose-estimation-adas-0001.bin"
        ),
        revision="Open Model Zoo 2021.4 / IR v10 / FP16",
        byte_size=3_823_814,
        note="Paired with head-pose-estimation-adas-0001.xml.",
        training_corpus_limitation=(
            "The pinned Open Model Zoo metadata says the network was trained from "
            "scratch for yaw, pitch, roll, and landmarks, but does not disclose a "
            "complete image-source, annotation, consent, or population manifest."
        ),
        legal_references=(OPEN_MODEL_ZOO_LICENSE,),
    ),
)
