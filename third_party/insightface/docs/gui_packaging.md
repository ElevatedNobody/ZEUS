# GUI Packaging and Distribution

InsightFace Evaluation Studio requires Python 3.10 or newer and supports
development installs, PyPI package installs, and desktop application builds.

## Development Install

```bash
cd python-package
pip install -e ".[gui]"
insightface-gui
```

The `gui` extra intentionally includes the `privateframe` dependency set
(`av` and `PyYAML`). A command-line/API-only installation can use
`pip install -e ".[privateframe]"` without installing PySide6.

This installs `onnxruntime` by default for CPU and supported macOS CoreML
systems. For an NVIDIA CUDA build, replace it after installing the GUI:

```bash
python -m pip uninstall -y onnxruntime
python -m pip install onnxruntime-gpu
```

Do not package both runtime distributions. Installing or upgrading the
InsightFace package may install `onnxruntime` again, so repeat this replacement
before building an NVIDIA artifact.

## Build Python Package

Set the next unpublished version in `insightface/__init__.py` before preparing
a release. Commit and push the intended source revision, then run
**Actions > Manual Python Package CI > Run workflow** for that revision and
wait for all jobs to pass. Build locally from the same revision with a clean
working tree and an active Python 3.10+ virtual environment:

```bash
cd python-package
python -m pip install build twine pytest
python -m pip install -e ".[gui]"
bash packaging/pypi/build_upload_pypi.sh --dry-run --python "$(command -v python)"
```

The release helper runs the complete test suite (GUI, PrivateFrame, models,
liveness, telemetry, and packaging), checks that the version is not already
on PyPI, and removes old `build/`, `dist/`, and top-level `*.egg-info` outputs.
It then builds in a fresh temporary directory, validates the wheel/source
distribution pair, checks metadata and packaged assets, and runs
`twine check --strict`. The validated files are copied to `dist/`.
**`--dry-run` does not upload anything.**

The GUI extra provides the dependencies for the complete test suite. Tests use
Qt's offscreen platform by default and do not require model downloads or local
test videos. `--no-clean` preserves old outputs and caches, so leave it off for
the normal release process. A direct `python -m build` is useful for development
but does not clean old outputs or run these release checks.

The default wheel is `insightface-<version>-py3-none-any.whl`: one pure-Python
wheel is shared by Windows, macOS, and Linux. The package requires Python
**3.10 or newer**; the manual CI currently tests Python **3.10, 3.11, and 3.12**
on all three operating systems. Availability on other Python versions and
platforms also depends on dependencies such as ONNX Runtime and PySide6.
Publish the accompanying `.tar.gz` source distribution with the wheel so
source builds remain available; they are two files for the same PyPI release.

## Optional face3d Extension

The default package does not compile the optional `face3d` Cython/C++
extension. This avoids requiring a C++ compiler for normal inference and GUI
users.

To manually enable it, install the optional dependencies and pass the explicit
build flag:

```bash
cd python-package
pip install -e ".[face3d]" --no-build-isolation --config-settings editable_mode=compat
python setup.py build_ext --inplace --with-face3d
```

Equivalent environment-variable control:

```bash
INSIGHTFACE_WITH_FACE3D=1 python setup.py build_ext --inplace
```

The chosen parameter name is `--with-face3d`.

## Upload PyPI

After the dry run above succeeds, upload those existing, validated files from
the same terminal, Python environment, and source revision. The commands below
assume the working directory is `python-package`. They read the source version
without importing InsightFace, confirm that `dist/` contains exactly the
matching wheel and source distribution, and upload those two filenames only.
Do not rebuild between validation and upload or use `dist/*` for uploading.

```bash
RELEASE_VERSION="$(python - <<'PY'
from pathlib import Path
import re

text = Path("insightface/__init__.py").read_text(encoding="utf-8")
match = re.search(r"^__version__\s*=\s*['\"]([^'\"]+)['\"]", text, re.M)
if match is None:
    raise SystemExit("Cannot read the source version")
print(match.group(1))
PY
)"
RELEASE_WHEEL="dist/insightface-${RELEASE_VERSION}-py3-none-any.whl"
RELEASE_SDIST="dist/insightface-${RELEASE_VERSION}.tar.gz"

python packaging/pypi/release_artifacts.py \
  --dist-dir dist --name insightface --version "$RELEASE_VERSION" >/dev/null &&
python packaging/pypi/artifact_smoke.py inspect \
  --wheel "$RELEASE_WHEEL" --sdist "$RELEASE_SDIST" &&
python -m twine check --strict "$RELEASE_WHEEL" "$RELEASE_SDIST" &&
python -m twine upload --repository pypi --username __token__ \
  "$RELEASE_WHEEL" "$RELEASE_SDIST"
```

Twine prompts for a PyPI API token if no credentials are already available.
Paste the complete `pypi-...` token at the prompt; it is not displayed while
typing. A saved `TWINE_PASSWORD`, `~/.pypirc` entry, or keyring credential may
allow upload without a prompt. Keep tokens out of command examples, source
files, and release records.

### Optional: Build and Upload in One Invocation

The same release helper can also rebuild, validate, ask for an explicit
confirmation, and upload in one invocation:

```bash
export TWINE_USERNAME=__token__
bash packaging/pypi/build_upload_pypi.sh --python "$(command -v python)"
```

This invocation **builds new artifacts**; it does not upload the files saved by
an earlier dry run. Use the manual upload above when the already-built files
are the ones approved for release.

The default release does not build `face3d`. For an intentionally separate
release with the optional extension enabled and its build prerequisites
installed, the helper also accepts:

```bash
bash packaging/pypi/build_upload_pypi.sh --with-face3d
```

An extension-enabled wheel is platform-specific; the `py3-none-any` filenames
and cross-platform description above apply to the default release only.

### Test and Release Notes

To run the complete test suite independently, from `python-package`:

```bash
QT_QPA_PLATFORM=offscreen python -m pytest -q tests
```

`pytest.ini` uses importlib collection so tests with the same filename in
different modules can run together. `--dry-run` runs tests and builds without
uploading; `--skip-tests` explicitly skips the test suite. Calling
`python -m build` or `twine upload` directly does not run these release tests.

The manual GitHub workflow checks artifacts, base/PrivateFrame/GUI
installations, installed-wheel GUI tests, and the complete source suite across
the matrix described above. It does not upload to PyPI or run on ordinary
pushes. If the source changes after CI or the local build, validate the new
revision before releasing it.

Only project maintainers or CI configured with PyPI Trusted Publisher should
upload official PyPI releases. Codex should not attempt to upload PyPI.

Before each release, confirm model licenses, README content, version numbers,
wheel contents, third-party notices, and that the package name is `insightface`.
PyPI versions are immutable, so a released version cannot be overwritten.

## Build Desktop Application

The first desktop packaging path uses PyInstaller one-folder mode.

Windows:

```powershell
cd python-package
pip install -e ".[gui]"
pip install pyinstaller
powershell -ExecutionPolicy Bypass -File packaging/desktop/build_windows.ps1
```

macOS:

```bash
cd python-package
pip install -e ".[gui]"
pip install pyinstaller
bash packaging/desktop/build_macos.sh
```

Linux:

```bash
cd python-package
pip install -e ".[gui]"
pip install pyinstaller
bash packaging/desktop/build_linux.sh
```

Output:

- Windows: `.exe` or `dist/InsightFace Evaluation Studio/`
- macOS: `.app`, with `.dmg` creation left as a release step
- Linux: executable directory, with AppImage/deb creation left as a release step

## Notes

- The GUI extra uses `PySide6-Essentials` for Qt Widgets and includes
  `reportlab` for PDF reports without installing the much larger
  `PySide6_Addons` wheel.
- `onnxruntime` and `onnxruntime-gpu` may require additional dynamic library
  work and must not coexist in one build environment.
- CUDA builds are not recommended for default community installers.
- A CPU provider build is the safest default.
- GPU/CUDA builds can be distributed as separate enterprise builds.
- Do not package user workspaces, SQLite databases, reports, images, videos, or
  embeddings.
- Do not package commercial model files by default.
- Desktop builds bundle `insightface/app/privateframe/configs/base.yaml` at the
  same package-relative path so the frozen GUI can load its single built-in
  PrivateFrame configuration without a source checkout.
- The GUI workspace/cache defaults to `~/.insightface/gui/`; model packages are
  manually downloaded into `~/.insightface/gui/cache/models` and extracted to
  `~/.insightface/models/<model_name>/`.
- Community builds must use PyInstaller one-folder mode, include third-party
  license notices, include LGPL license text, include Qt/PySide6 source offer,
  and must not restrict replacement of Qt/PySide6 shared libraries.
