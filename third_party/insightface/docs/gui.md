# InsightFace Evaluation Studio GUI

InsightFace Evaluation Studio is a local desktop GUI for InsightFace 2.0. It is
designed for local video face privacy with PrivateFrame, no-code face recognition
testing, local People Library management, album organization, enterprise model
evaluation, report export, and basic face swap trials. See the
[PrivateFrame guide](../insightface/app/privateframe/README.md) for its full
workflow and video demo.

## Install

```bash
cd python-package
pip install -e ".[gui]"
insightface-gui
```

PyPI users can install:

```bash
pip install "insightface[gui]"
insightface-gui
```

These commands install `onnxruntime` by default for CPU and supported macOS
CoreML systems. The `gui` extra also installs PrivateFrame, PyAV, and PyYAML.
PrivateFrame can instead be installed without the GUI with
`pip install "insightface[privateframe]"`. For NVIDIA CUDA, replace the default
runtime after installing the GUI:

```bash
python -m pip uninstall -y onnxruntime
python -m pip install onnxruntime-gpu
```

Do not keep `onnxruntime` and `onnxruntime-gpu` in the same environment.
Installing or upgrading `insightface[gui]` may install the default runtime
again, so repeat this replacement afterward on NVIDIA systems.

Aliases:

```bash
insightface-eval-studio
insightface-desktop
python -m insightface.gui
```

## Workspace

By default, user data is stored in:

```text
~/.insightface/gui
```

The workspace contains the SQLite database, crops, exports, reports, and logs.
The GUI does not upload images, videos, embeddings, or reports automatically.

You can override the workspace:

```bash
insightface-gui --workspace /path/to/workspace
```

## File, Folder, and Video Inputs

Image and video pages use the preview frame as the upload target. Click the
empty preview frame labeled `Click to upload or drag a file here`, or drag a
local file onto it. The preview changes color on hover to indicate it is
clickable, and changes again while a valid file is dragged over it. After a file
is loaded, the small `×` button removes it, and dragging another file onto the
preview replaces it.

Multi-file imports, folder imports, CSV files, and local model inputs use
clickable drag-and-drop selectors with hover and drag-over color feedback.

## Mode-based navigation

InsightFace Evaluation Studio uses five workflow modes. The mode selector is a
persistent **Workflows** rail on the left side of the window so the current
workspace is always visible. Face Recognition, Album Management, and Face Swap
use a single full-width workspace; modes with several workflows show a compact
secondary sidebar next to the workflow rail:

The Workflows rail also keeps the local-processing notice visible in every
mode: **All processing is local. No images, embeddings, or reports are uploaded
automatically.**

1. **PrivateFrame**: upload or drop a video and use the global model, model root,
   and provider selected under **Models**. PrivateFrame enables processing only
   for `raccoon_s` or `raccoon_l`; a missing Raccoon package can be downloaded
   to the displayed global model root on first use, while an invalid installed
   V2 package is reported before processing starts. Keep **Fast (default, target
   15 analysis FPS)** or choose **Normal (target 30 analysis FPS)** for denser
   sampling, then select a privacy policy and Gaussian / Mosaic. These are
   regular detection sampling targets, not output FPS or processing speed.
   Processing runs through the PrivateFrame Python API on a background worker.
   The output directory initially uses the
   operating system's Videos directory (Movies on macOS), and remains editable.
   The page always writes
   `<video>_privateframe.json` to the selected output directory. Choose
   **JSON only** to stop after analysis for later editing, or **JSON + redacted
   video** to also write the paired `<video>_privateframe.mp4`. **More Options**
   contains between-scan control, face coverage, encoding preset (Medium by
   default), CRF quality, supported audio, and person-matching settings. Photo
   modes use one reference-photo folder with no per-person subfolders; only
   the largest face in each photo is used. **Blur only** blurs matched people
   and leaves unmatched or uncertain people visible; **Exempt** keeps matched
   people visible and blurs everyone else. Review the output before sharing it.
2. **Face Recognition**: one Query & Gallery workspace. One gallery image runs
   1:1 compare; multiple gallery images or a folder run 1:N gallery search.
3. **Album Management**: one **Album** workspace for local folder import,
   refresh, DBSCAN face clustering, and photo review.
4. **Face Swap**: one Source + Target = Result workspace. Target can be an
   image or video.
5. **Enterprise Evaluation**: a single local 1:1 / 1:N evaluation workspace
   with identity-folder import, Auto Split, metrics, and report export.

Global utilities are always available from the top app bar:

- **Settings** opens the application settings dialog for the UI theme and
  language. Language defaults to the operating system when supported, otherwise
  English. Supported GUI languages are English, Chinese, Japanese, Korean,
  Spanish, French, German, Portuguese, and Russian. The included themes are
  System, Precision Light, Studio Dark, Graphite Pro, Azure Lab, Emerald Focus,
  and Crimson Audit. Workspace paths are chosen on first launch and are not
  changed from this dialog.
- **Models** opens runtime settings, model downloads, and custom model directory
  tools.
- **License** opens the License Center dialog.

Settings, Model Settings, Model Downloads, and License Center are intentionally
not shown in the left sidebar.

## Models

Open **Models** from the top app bar or **Tools > Models** to choose:

- global model pack: `raccoon_s`, `raccoon_l`, `buffalo_l`, `buffalo_m`,
  `buffalo_s`, `buffalo_sc`, `antelopev2`, or a custom model folder
- global InsightFace model root (the parent of `models/<model_name>`)
- provider: Auto, CPU, CUDA when `CUDAExecutionProvider` is available
- detection size: Auto, 128x128, 320x320, 640x640, 1024x1024
- face swap model
- GFPGAN post-processing model, if `GFPGANv1.4.onnx` has been downloaded

**Auto** detection size is the default. It runs joint 128x128 and 640x640
detection and merges duplicate boxes.

The Runtime tab also lets you choose a face swap model. Only downloaded local
swap models are listed. Download `inswapper_128.onnx` or another compatible
swap model from **Models > Downloads** first.

The Runtime tab can also enable GFPGAN post-processing for face swap. GFPGAN is
listed in **Models > Downloads** as a third-party restore model and is not
downloaded automatically. When enabled, face swap output is passed through the
configured GFPGAN ONNX model using a 512x512 restore pass.

New GUI installations default to `raccoon_s`, so the initially selected
PrivateFrame workflow can run without a separate model selection. Existing
`config.json` files keep their saved model selection.

The GUI opens even when a model is missing. In that case, pages show
`Model is not loaded. Please open Models.`

The general face-recognition and face-swap GUI does not download models
automatically. Open **Models > Downloads** and click **Refresh Download URLs**
to fetch the dedicated `model-zoo` release assets from:

```text
https://github.com/deepinsight/insightface/releases/tag/model-zoo
```

InsightFace assets resolve under
`https://github.com/deepinsight/insightface/releases/download/model-zoo/`.
Cached official URLs from the former `v0.7` release are migrated when loaded.

The refreshed URLs are cached in:

```text
~/.insightface/gui/cache/model_download_urls.json
```

The GFPGANv1.4 third-party entry is added by the GUI alongside refreshed
InsightFace release assets. Its separate third-party release URL is unchanged,
and the file is saved under the same local model root after manual download.

Downloaded archives are cached in:

```text
~/.insightface/gui/cache/models
```

PrivateFrame is the exception: when the global model is `raccoon_s` or
`raccoon_l`, it may be downloaded under the configured global model root on
first use. PrivateFrame never silently falls back to `~/.insightface` or to a
different Raccoon package.

Zip model packages are extracted below the configured model root. With the
default root, that is:

```text
~/.insightface/models/<model_name>/
```

For example:

```text
~/.insightface/models/buffalo_l/
~/.insightface/models/antelopev2/
```

Users can also manually place model directories under the configured
`<model_root>/models/` directory or choose **custom model directory** from
**Models > Runtime** to reveal the custom directory field. Choosing a catalog
package clears the custom-directory override; choosing a custom directory saves
that non-empty path as both the model identity and explicit model directory.

Changing the global model settings invalidates the ordinary GUI FaceAnalysis
engine and refreshes PrivateFrame. A PrivateFrame job already in progress keeps
the model name, model root, and provider snapshot captured when it started; the
new global selection applies to the next job. Runtime Sessions are not shared
between the ordinary GUI and PrivateFrame. Within one GUI process, model
downloads and PrivateFrame processing are mutually exclusive: a download keeps
PrivateFrame disabled even if the Models dialog is closed, and Models cannot be
opened until a running PrivateFrame job finishes.

## Face Recognition

Open **Face Recognition** and use the full-width **Query & Gallery** workspace.
The Query preview accepts one image by click or drag. The Gallery panel accepts
one image, multiple images, or a folder by click or drag. There is no separate
Choose button in Gallery; click the Gallery panel or drag files/folders onto it.

If Gallery contains one image, **Run Recognition** automatically runs 1:1
compare and reports similarity, threshold, decision, and detection score. If
Gallery contains multiple images or a folder, the same button runs 1:N gallery
search and ranks the gallery images by similarity.

## Multi-face Photo Recognition

Open **Multi-face Photo Recognition** to detect all faces in a group photo,
identify them against People Library, save results to the local database, and
export annotated images or CSV/JSON.

## Batch Processing

Open **Batch Folder Processing**, select an image folder, and choose recursive
scan, crop saving, and identification options. Batch results can be exported to
CSV and JSON.

## Album People Clustering

Open **Album Management > Album** to add one or more album directories. Click
**Import / Refresh** to scan new image files, detect faces, save local crops,
and cluster all indexed faces from the selected directories. The page exposes a
cosine similarity threshold with a default value of `0.48`; higher values make
clusters stricter. DBSCAN internally receives cosine distance as
`1 - cosine threshold`. The GUI extra installs scikit-learn for DBSCAN
clustering.

Album cluster IDs avoid duplicating existing People Library IDs. When a cluster
matches an existing person within the configured duplicate distance threshold
(`0.28` by default), the existing person ID is reused; otherwise the page assigns
the next available album person ID. The cluster thumbnail is chosen from the
face nearest the cluster centroid. Selecting a cluster shows all original photo
thumbnails for that cluster, and double-clicking a thumbnail opens the original
image.

Album directories and clustering results are saved in the local SQLite
database so the page can restore them on the next launch. **Clear** only clears
the selected album directories and leaves the current clustering results
visible. **Rebuild All** asks for confirmation, then reprocesses all selected
album directories from scratch and replaces the saved clustering results. If no
album directories are selected, **Rebuild All** clears the saved clustering
results.

## Enterprise Evaluation

Open **Enterprise Evaluation** to run no-code local evaluations:

- **1:1 Verification** from identity folders. Each subfolder is one identity.
  With **Auto Split**, the file containing `gallery` or the first sorted image
  becomes that identity's gallery image; other images are probes. Matching is
  probe-vs-gallery across identities. Without Auto Split, all images are probes
  and the page runs full pairwise probe-vs-probe comparisons.
- **1:N Identification** from identity folders. With **Auto Split**, select a
  dataset containing `identities/<identity folders>` or identity folders
  directly; gallery images are selected with the same rule as 1:1. Without Auto
  Split, use `gallery/<identity>`, `probe/<identity>`, and optional `unknown/`
  folders.

Choose a **Multi-face handling** policy before validation:

- **Require exactly one face**: default. Validation fails if any evaluation
  image contains more than one detected face.
- **Use largest face**: validation warns on multi-face images and evaluation
  uses the largest detected face.
- **Use largest centered face**: validation warns on multi-face images and
  evaluation uses the face with the best area-minus-center-distance score.
- **Mark as skip**: multi-face images are skipped. If a required gallery image
  would be skipped, validation fails because that identity no longer has a
  usable gallery sample.

Click **Validate Dataset** before running. Validation checks the folder layout,
Auto Split rules, gallery/probe availability, generated positive and negative
pairs for 1:1, required gallery coverage for 1:N, and the selected multi-face
policy. **Run Evaluation** is enabled only after validation passes.

1:1 reports include best cosine threshold accuracy, the threshold at that
operating point, and TAR@FAR for `1e-6`, `1e-5`, `1e-4`, and `1e-3`. 1:N
reports include Top1 plus TAR@FAR for `1e-5`, `1e-4`, `1e-3`, and `1e-2`,
including the corresponding thresholds.

## Report Export

Reports are written to:

```text
~/.insightface/gui/reports
```

Markdown, HTML, and PDF are supported when the GUI extra is installed.

## Face Swap

Open **Face Swap** to use the full-width Source + Target = Result workspace.
Choose the swap model in **Models > Runtime**; the page itself does not expose a
model picker. The swap model is loaded only when you click **Run Face Swap**.
If GFPGAN post-processing is enabled in **Models > Runtime**, the swapped image
or swapped video frames are restored with GFPGAN before being saved.

Source is always an image. Target can be an image or a video, and the workflow
automatically chooses image swap or video swap from the target file type. Video
swap writes an `.mp4` result to the exports folder and shows a preview frame
when one is available.

If the model is missing, it shows:

```text
Face swap model not found. Please download and choose a swap model in Models.
```

Face swap may require separate commercial authorization depending on usage and
model license. Use only with appropriate rights and consent.

## License Notice

Code and model files may have different licenses. Research or publicly
distributed pretrained models may be restricted to non-commercial or research
usage. Commercial deployment requires appropriate model authorization.

This tool does not provide legal advice. Users are responsible for consent,
privacy, retention, and compliance with applicable biometric regulations.

## Troubleshooting

Run without automatic model loading:

```bash
insightface-gui --safe-mode
```

Safe mode is only applied to the current launch. It is intended for
troubleshooting model/provider issues and is not saved to `config.json`.

Force CPU:

```bash
insightface-gui --provider cpu
```

`CUDA` is selectable only when ONNX Runtime reports `CUDAExecutionProvider`.
If CUDA is requested on a machine without a usable CUDA provider, the GUI
falls back to Auto/CPU instead of exposing a broken GPU option.

Logs are stored in:

```text
~/.insightface/gui/logs/app.log
```
