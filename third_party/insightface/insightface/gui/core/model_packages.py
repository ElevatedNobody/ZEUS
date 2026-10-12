"""Shared GUI model-package choices and PrivateFrame compatibility checks."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ...model_zoo.package_manifest import (
    DETECTION_TASK,
    MODEL_PACKAGE_MANIFEST,
    RECOGNITION_TASK,
    SUPPORTED_MANIFEST_PACKAGES,
    VERIFICATION_TASK,
    load_model_package,
)


# Keep one ordered catalog for the first-launch wizard, runtime settings, and
# download actions.  The first entry is also the default for a new GUI config.
GUI_MODEL_PACKAGES = (
    *SUPPORTED_MANIFEST_PACKAGES,
    "buffalo_l",
    "buffalo_m",
    "buffalo_s",
    "buffalo_sc",
    "antelopev2",
)
PRIVATEFRAME_MODEL_PACKAGES = frozenset(SUPPORTED_MANIFEST_PACKAGES)
CUSTOM_MODEL_CHOICE = "__custom_model_directory__"


@dataclass(frozen=True)
class PrivateFrameModelStatus:
    """Read-only readiness result for the current global GUI model selection."""

    model_name: str
    model_root: Path
    package_path: Path
    state: str
    can_start: bool
    message: str

    @property
    def installed(self) -> bool:
        return self.state == "ready"


def model_package_path(model_name: str, model_root: str | Path) -> Path:
    return Path(model_root).expanduser().resolve() / "models" / str(model_name)


def is_gui_model_package_asset(*, name: str, source: str) -> bool:
    """Return whether a download-catalog row may become the global GUI model."""

    path = Path(str(name))
    return (
        str(source).casefold() == "insightface"
        and path.suffix.casefold() == ".zip"
        and path.stem in GUI_MODEL_PACKAGES
    )


def inspect_privateframe_model(
    model_name: str,
    model_root: str | Path,
    *,
    require_recognition: bool = False,
) -> PrivateFrameModelStatus:
    """Inspect a Raccoon package without downloading it or creating Sessions.

    A completely absent supported package remains runnable because PrivateFrame
    intentionally lets ModelZoo download it on first use.  Once a package
    directory exists, however, it must be a usable V2 package; silently falling
    back or downloading over a partial/corrupt directory would hide a bad local
    installation.
    """

    normalized_name = str(model_name or "").strip()
    root_text = str(model_root or "").strip()
    root = Path(root_text or ".").expanduser().resolve()
    package_path = root / "models" / normalized_name
    if normalized_name not in PRIVATEFRAME_MODEL_PACKAGES:
        return PrivateFrameModelStatus(
            model_name=normalized_name,
            model_root=root,
            package_path=package_path,
            state="unsupported",
            can_start=False,
            message=(
                "PrivateFrame supports only raccoon_s or raccoon_l. "
                "Open Models and select a Raccoon package."
            ),
        )
    if not root_text:
        return _invalid_privateframe_status(
            normalized_name,
            root,
            package_path,
            "the model root is empty",
        )
    if root.exists() and not root.is_dir():
        return _invalid_privateframe_status(
            normalized_name,
            root,
            package_path,
            f"the model root is not a directory: {root}",
        )
    models_path = root / "models"
    if models_path.exists() and not models_path.is_dir():
        return _invalid_privateframe_status(
            normalized_name,
            root,
            package_path,
            f"the models path is not a directory: {models_path}",
        )
    if not package_path.exists():
        return PrivateFrameModelStatus(
            model_name=normalized_name,
            model_root=root,
            package_path=package_path,
            state="missing",
            can_start=True,
            message=(
                f"{normalized_name} is not installed under {root / 'models'}. "
                "It will be downloaded there on first use."
            ),
        )
    if not package_path.is_dir():
        return _invalid_privateframe_status(
            normalized_name,
            root,
            package_path,
            f"the package path is not a directory: {package_path}",
        )

    manifest_path = package_path / MODEL_PACKAGE_MANIFEST
    if not manifest_path.is_file():
        return _invalid_privateframe_status(
            normalized_name,
            root,
            package_path,
            f"the V2 manifest is missing: {manifest_path}",
        )
    try:
        package = load_model_package(package_path)
    except Exception as exc:
        return _invalid_privateframe_status(
            normalized_name,
            root,
            package_path,
            str(exc),
        )
    if package.name != normalized_name:
        return _invalid_privateframe_status(
            normalized_name,
            root,
            package_path,
            f"manifest model_id is {package.name!r}",
        )

    required_tasks = [DETECTION_TASK, VERIFICATION_TASK]
    if require_recognition:
        required_tasks.append(RECOGNITION_TASK)
    missing_tasks = [task for task in required_tasks if task not in package.tasks]
    if missing_tasks:
        return _invalid_privateframe_status(
            normalized_name,
            root,
            package_path,
            "missing required task(s): " + ", ".join(missing_tasks),
        )
    missing_files = []
    for task in required_tasks:
        descriptor = package.tasks[task]
        if not descriptor.path.is_file():
            missing_files.append(str(descriptor.path))
    if missing_files:
        return _invalid_privateframe_status(
            normalized_name,
            root,
            package_path,
            "missing model file(s): " + ", ".join(missing_files),
        )
    try:
        for task in required_tasks:
            descriptor = package.tasks[task]
            if descriptor.sha256 is None:
                continue
            stat = descriptor.path.stat()
            actual_sha256 = _cached_sha256(
                str(descriptor.path),
                stat.st_size,
                stat.st_mtime_ns,
            )
            if actual_sha256 != descriptor.sha256:
                raise RuntimeError(
                    f"model SHA-256 mismatch for {descriptor.path}: expected "
                    f"{descriptor.sha256}, got {actual_sha256}"
                )
    except (OSError, RuntimeError) as exc:
        return _invalid_privateframe_status(
            normalized_name,
            root,
            package_path,
            str(exc),
        )
    return PrivateFrameModelStatus(
        model_name=normalized_name,
        model_root=root,
        package_path=package_path,
        state="ready",
        can_start=True,
        message=f"Ready: {normalized_name} from {package_path}.",
    )


def _invalid_privateframe_status(
    model_name: str,
    model_root: Path,
    package_path: Path,
    reason: str,
) -> PrivateFrameModelStatus:
    return PrivateFrameModelStatus(
        model_name=model_name,
        model_root=model_root,
        package_path=package_path,
        state="invalid",
        can_start=False,
        message=f"PrivateFrame cannot use {model_name}: {reason}",
    )


@lru_cache(maxsize=32)
def _cached_sha256(path: str, size: int, mtime_ns: int) -> str:
    """Hash an unchanged artifact once across repeated GUI page refreshes."""

    del size, mtime_ns
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "CUSTOM_MODEL_CHOICE",
    "GUI_MODEL_PACKAGES",
    "PRIVATEFRAME_MODEL_PACKAGES",
    "PrivateFrameModelStatus",
    "inspect_privateframe_model",
    "is_gui_model_package_asset",
    "model_package_path",
]
