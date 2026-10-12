# Model Zoo and model usage

[Back to the Python library](../README.md)

InsightFace loads ONNX models for face analysis and direct detection or
recognition use. See the [runtime guide](runtime.md) for installation and
execution provider selection.

## Model licenses

The InsightFace Python library code is released under the MIT License and
can be used for academic and commercial purposes.

**The pretrained models provided with this library are available for
non-commercial research purposes only, including both automatically and
manually downloaded models.** To use your own licensed models, see
[Use your own licensed model](#use-your-own-licensed-model).

## Raccoon model packages

InsightFace 2.0 supports `raccoon_s` and `raccoon_l` through task-aware V2
manifests that declare the model files and preprocessing. Use them with
`FaceAnalysis(name="raccoon_s")` or `FaceAnalysis(name="raccoon_l")`.
PrivateFrame requires one of these packages; its default and the default for
new GUI configurations is `raccoon_s`. Ordinary `FaceAnalysis()` continues to
use `buffalo_l` by default.

Model packages are stored under `<root>/models/<name>/` (default root:
`~/.insightface`). PrivateFrame can download a missing selected package on
first use; see its [model setup guide](../insightface/app/privateframe/README.md).
Liveness is a separate optional addon and must be enabled explicitly; see the
[liveness guide](liveness.md) for installation and usage.

## Legacy model packs

The library also provides the following model packs. The name in **bold** is
the default for ordinary `FaceAnalysis()`. **Auto** indicates whether the
Python library can download the pack directly.

After manually downloading a model pack ZIP, unzip it under
`~/.insightface/models/` before running the program, so its model files are
in `~/.insightface/models/<name>/`.

| Name | Detection Model | Recognition Model | Alignment | Attributes | Model-Size | Link | Auto |
| --- | --- | --- | --- | --- | --- | --- | --- |
| antelopev2 | SCRFD-10GF | ResNet100@Glint360K | 2d106 & 3d68 | Gender&Age | 407MB | [Download](https://drive.google.com/file/d/18wEUfMNohBJ4K3Ly5wpTejPfDzp-8fI8/view?usp=sharing) | N |
| **buffalo_l** | SCRFD-10GF | ResNet50@WebFace600K | 2d106 & 3d68 | Gender&Age | 326MB | [Download](https://drive.google.com/file/d/1qXsQJ8ZT42_xSmWIYy85IcidpiZudOCB/view?usp=sharing) | Y |
| buffalo_m | SCRFD-2.5GF | ResNet50@WebFace600K | 2d106 & 3d68 | Gender&Age | 313MB | [Download](https://drive.google.com/file/d/1net68yNxF33NNV6WP7k56FS6V53tq-64/view?usp=sharing) | N |
| buffalo_s | SCRFD-500MF | MBF@WebFace600K | 2d106 & 3d68 | Gender&Age | 159MB | [Download](https://drive.google.com/file/d/1pKIusApEfoHKDjeBTXYB3yOQ0EtTonNE/view?usp=sharing) | N |
| buffalo_sc | SCRFD-500MF | MBF@WebFace600K | - | - | 16MB | [Download](https://drive.google.com/file/d/19I-MZdctYKmVf3nu5Da3HS6KH5LBfdzG/view?usp=sharing) | N |

### Recognition accuracy

| Name | MR-ALL | African | Caucasian | South Asian | East Asian | LFW | CFP-FP | AgeDB-30 | IJB-C(E4) |
| :--- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| buffalo_l | 91.25 | 90.29 | 94.70 | 93.16 | 74.96 | 99.83 | 99.33 | 98.23 | 97.25 |
| buffalo_s | 71.87 | 69.45 | 80.45 | 73.39 | 51.03 | 99.70 | 98.00 | 96.58 | 95.02 |

`buffalo_m` has the same recognition accuracy as `buffalo_l`.
`buffalo_sc` has the same recognition accuracy as `buffalo_s`.

### Automatic downloads

For `insightface>=0.3.3`, initializing `app = FaceAnalysis()` automatically
downloads the default model package when it is missing. Automatic ModelZoo
downloads use the dedicated
[`model-zoo` release](https://github.com/deepinsight/insightface/releases/tag/model-zoo).

### Legacy download command for 0.3.2

For `insightface==0.3.2`, download the model package before using it:

```bash
insightface-cli model.download buffalo_l
```

## Use your own licensed model

Create a new model directory under `~/.insightface/models/` and place your own
compatible ONNX models there in place of the pretrained models provided by
InsightFace. For example, models in
`~/.insightface/models/your_model_zoo/` can be loaded with:

```python
from insightface.app import FaceAnalysis

app = FaceAnalysis(name="your_model_zoo")
```

## Call models

The library supports ONNX models. Detection or recognition models trained
with PyTorch, MXNet, or another framework must be converted to compatible ONNX
models before loading them with InsightFace.

### Call detection models

```python
import insightface
from insightface.app import FaceAnalysis

# Method 1: use FaceAnalysis with only the detection module enabled.
app = FaceAnalysis(allowed_modules=["detection"])
app.prepare()  # ctx_id=0; Auto detection size: 128x128 + 640x640

# Method 2: load a detection model directly.
detector = insightface.model_zoo.get_model("your_detection_model.onnx")
detector.prepare(ctx_id=0)  # SCRFD defaults to Auto: 128x128 + 640x640
```

### Call recognition models

```python
import insightface

handler = insightface.model_zoo.get_model("your_recognition_model.onnx")
handler.prepare(ctx_id=0)
```
