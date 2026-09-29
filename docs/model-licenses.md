# Optional model licenses and provenance

Models are optional downloads, not part of Pandect's Apache-2.0 grant. No
weights are distributed in this repository. The authoritative filenames,
revisions, transfer URLs, sizes, full SHA-256 digests, and legal references are
in [`model_manifest.py`](../pandect/processing/model_manifest.py). Inspect the
same inventory without downloading:

```console
make manage ARGS="provision_processing_models --list"
```

The table below summarizes the **specific pinned artifacts**, not an entire
model family. An upstream software license and an archived license text do
not establish training-data rights, subject consent, freedom from bias, or
commercial clearance. Pandect cannot independently establish those rights.
No statement here determines that a dataset's terms necessarily apply to a
particular weight, or that a weight's permissive label clears all upstream rights.

## Artifact inventory

| Pinned artifact | Source / revision | Archived legal basis | Known provenance limitation |
| --- | --- | --- | --- |
| `face_detection_yunet_2023mar.onnx` | OpenCV Zoo `47534e27`, March 2023 | Model-local **MIT**, archive `yunet-mit-c83b8120.txt` | Upstream reports WIDER Face validation and links training code; no complete weight-bound training-sample manifest establishes licensing, demographic coverage, or preprocessing. |
| `face_recognition_sface_2021dec.onnx` | OpenCV Zoo `47534e27`, December 2021 | Model-local **Apache-2.0**, archive `apache-2.0-cfc7749b.txt` | Architecture/paper links are not a complete corpus manifest; identity consent, demographic coverage, and exact preprocessing remain undisclosed. |
| `facial_expression_recognition_mobilefacenet_2022july.onnx` | OpenCV Zoo `47534e27`, July 2022 | Repository **Apache-2.0**, archive `apache-2.0-c71d239d.txt`; **no model-local license exists at the pinned revision despite its README link** | Repository-license fallback is disclosed, not additional model-specific clearance. No complete training-corpus or label-provenance manifest; demographic coverage and expression-label construction remain undisclosed. |
| `3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx` | sherpa-onnx `speaker-recongition-models` release, SHA-256-pinned export | 3D-Speaker **Apache-2.0** (`apache-2.0-c71d239d.txt`) and distributor **Apache-2.0** (`apache-2.0-cfc7749b.txt`) | Filename identifies a bilingual zh/en CAM++ export, but not a complete artifact-bound corpus/model card; consent, licensing, language balance, and preprocessing remain undisclosed. |
| `silero_vad_v4_9e2449e1.onnx` | sherpa-onnx `asr-models` release, SHA-256-pinned Silero v4 export | Silero **MIT** (`silero-vad-mit-2e63e9a3.txt`) and distributor **Apache-2.0** (`apache-2.0-cfc7749b.txt`) | No complete auditable training manifest establishes language, acoustic-domain, consent, or data-license coverage. |
| `age-gender-recognition-retail-0013.xml` and `.bin` | Open Model Zoo 2021.4, IR v10, FP16 | Open Model Zoo **Apache-2.0**, archive `apache-2.0-c71d239d.txt` | No complete image-source, annotation, consent, or demographic training manifest. Pandect exposes broad apparent-age bands and anonymous binary visual scores, not sex/gender identity. |
| `head-pose-estimation-adas-0001.xml` and `.bin` | Open Model Zoo 2021.4, IR v10, FP16 | Open Model Zoo **Apache-2.0**, archive `apache-2.0-c71d239d.txt` | Upstream describes training from scratch for pose/landmarks, but supplies no complete image-source, annotation, consent, or population manifest. |

Both the graph and weight file are required for each OpenVINO model. Partial
provisioning does not silently reduce a stage's promised inference: missing or
invalid required artifacts cause visible failures. Catalogue storage and
deterministic extraction do not require accepting model licenses.

## Exact archived texts

The archive location is `<data>/models/legal/`. Multiple sources may contain
the same exact bytes, but their artifact-specific references remain in the
manifest and provisioning notice.

| Archive | SHA-256 | Bytes |
| --- | --- | ---: |
| `yunet-mit-c83b8120.txt` | `c83b8120c50ccbd4c4f96edf53141bdd566ebb8f8e9227e415326aa1b1aba958` | 1,085 |
| `silero-vad-mit-2e63e9a3.txt` | `2e63e9a38b6e8fc0c7bc37ce174caca1862870856c6daf5697cfb785e925520b` | 1,075 |
| `apache-2.0-c71d239d.txt` | `c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4` | 11,357 |
| `apache-2.0-cfc7749b.txt` | `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30` | 11,358 |

Pinned upstream texts:

- OpenCV Zoo: [YuNet model license](https://raw.githubusercontent.com/opencv/opencv_zoo/47534e27c9851bb1128ccc0102f1145e27f23f98/models/face_detection_yunet/LICENSE),
  [SFace model license](https://raw.githubusercontent.com/opencv/opencv_zoo/47534e27c9851bb1128ccc0102f1145e27f23f98/models/face_recognition_sface/LICENSE),
  [repository license used for expression](https://raw.githubusercontent.com/opencv/opencv_zoo/47534e27c9851bb1128ccc0102f1145e27f23f98/LICENSE).
- [3D-Speaker](https://raw.githubusercontent.com/modelscope/3D-Speaker/b6ea24f428a178c02b7ef9d6900569050448c8a1/LICENSE)
  and [sherpa-onnx distributor](https://raw.githubusercontent.com/k2-fsa/sherpa-onnx/142807252687d81b40d6315f23470a1512a00de3/LICENSE).
- [Silero VAD](https://raw.githubusercontent.com/snakers4/silero-vad/915dd3d639b8333a52e001af095f87c5b7f1e0ac/LICENSE)
  and [Open Model Zoo](https://raw.githubusercontent.com/openvinotoolkit/open_model_zoo/e37a163a2385d9ddaa8e3da1ccb50c80d4fa0802/LICENSE).

## Operator decision and record

Review the inventory and upstream terms before explicitly accepting:

```console
make models-provision ACCEPT_MODEL_LICENSES=yes
make model-smoke
```

Provisioning verifies the exact legal bytes before installing weights and
preserves an existing mismatched legal file instead of overwriting it. The
cumulative `PROCESSING-MODELS-NOTICE.txt` records each accepted artifact set,
revisions, legal URLs/digests, and corpus limitations; existing notes and prior
records are retained. Keep the notice and legal archives with model backups.

Public verified weights/legal files become readable to sandbox job identities;
the surrounding data directory and operator-annotated provisioning notice stay
private. Unknown model-directory contents are not made public recursively.
See [model provisioning](operations.md#local-model-provisioning) for migration
and remediation.

Provisioning contacts the listed GitHub/OpenVINO hosts (and their download
redirects), which receive normal download-request metadata such as the host IP.
It does not upload catalogue content. Runtime tasks, startup, health checks,
and ordinary tests do not download models. Actual model smoke tests are an
explicit additional validation using synthetic inputs, not proof of accuracy
or suitability on your evidence.
