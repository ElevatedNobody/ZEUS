# Third-party code shipped with AGNI

Both folders are unmodified copies of the repositories supplied for AGNI on
2026-09-23 (`insightface-master.zip`, `wf-main.zip`).

## `insightface/` - InsightFace Python library 2.0

* Source: https://github.com/deepinsight/insightface (`python-package/`).
  Tests and packaging scripts were left out; nothing else was changed.
* Code licence: **MIT** (stated in the package README and `setup.py`).
* **Pretrained models: non-commercial research use only.** The InsightFace
  README says the models it provides, downloaded automatically or manually,
  are for non-commercial research purposes only. AGNI does not ship the
  models; `SETUP_AGNI.bat` downloads `buffalo_l` and `liveness.onnx` from the
  official release pages and checks each against the SHA-256 GitHub publishes.
  For commercial use, obtain a commercial model licence from InsightFace or
  use your own licensed models (`[face] model_pack` in config.ini).
* Used by: `security/face_auth.py` - face detection (SCRFD), recognition
  (ArcFace, 512-d templates) and RGB liveness, loaded from
  `models/insightface/` only. Face login is an optional second factor.

## `wf/` - tailscale/wf (Windows Filtering Platform)

* Source: https://github.com/tailscale/wf
* Licence: **BSD-3-Clause** (`wf/LICENSE`).
* Used by: `tools/agni-wfp/main.go`, compiled to `bin/agni-wfp.exe`
  (Go 1.27.1, `-trimpath`, no cgo). `security/wfp.py` runs it; applying or
  removing filters always needs an administrator.

Rebuilding the helper (needs Go):

    cd tools\agni-wfp
    go build -trimpath -ldflags "-s -w" -o ..\..\bin\agni-wfp.exe .
