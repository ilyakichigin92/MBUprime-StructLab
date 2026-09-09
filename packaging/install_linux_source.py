"""Transactional Ubuntu x86-64 source installer for MBUprime StructLab."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import uuid


PRODUCT = "mbuprime-structlab"
VERSION_PATTERN = re.compile(r'^__version__\s*=\s*"([^"]+)"\s*$', re.MULTILINE)
INSTALL_MARKER = ".mbuprime-structlab-install.json"
STAGE_MARKER = ".mbuprime-structlab-stage.json"
MANAGED_LINE = "# mbuprime-structlab-managed-v1"
RUNTIME_LOCK = "requirements-targets/ubuntu-24.04-x86_64.txt"
BUILD_LOCK = "requirements-targets/ubuntu-source-build.txt"
LIVE_INPUT = b"name\trole\tsequence\ninstall_check\tprimer\tGCCCACAGGACGTCAAGTTCC\n"


class InstallError(RuntimeError):
    """The requested installation would be unsafe or incomplete."""


@dataclass(frozen=True)
class Layout:
    configured_prefix: Path
    destdir: Path | None
    actual_prefix: Path
    product_root: Path
    version_root: Path
    current_link: Path
    cli_launcher: Path
    gui_launcher: Path
    desktop_entry: Path


def _source_version(source_root: Path) -> str:
    version_file = source_root / "mbuprime_structlab" / "__init__.py"
    pyproject_file = source_root / "pyproject.toml"
    try:
        contents = version_file.read_text(encoding="utf-8")
        project = tomllib.loads(pyproject_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise InstallError(f"cannot read application version: {exc}") from exc
    match = VERSION_PATTERN.search(contents)
    if match is None or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", match.group(1)):
        raise InstallError("application version is missing or invalid")
    package_version = match.group(1)
    project_version = project.get("project", {}).get("version")
    if project_version != package_version:
        raise InstallError(
            "version mismatch between pyproject.toml and "
            f"mbuprime_structlab/__init__.py: {project_version!r} != "
            f"{package_version!r}")
    return package_version


def _absolute_path(value: str, label: str, *, empty_ok: bool = False) -> Path | None:
    if not value and empty_ok:
        return None
    if not value or "\n" in value or "\r" in value or "\0" in value:
        raise InstallError(f"{label} must be a non-empty absolute path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise InstallError(f"{label} must be absolute: {value!r}")
    resolved = path.resolve(strict=False)
    if resolved == Path(resolved.anchor):
        raise InstallError(f"{label} may not be the filesystem root")
    return resolved


def layout_for(prefix: str, destdir: str, version: str) -> Layout:
    configured = _absolute_path(prefix, "PREFIX")
    destination = _absolute_path(destdir, "DESTDIR", empty_ok=True)
    assert configured is not None
    if destination is None:
        actual = configured
    else:
        # DESTDIR stages the configured absolute hierarchy; it is never
        # embedded in launchers or desktop metadata.
        relative = Path(*configured.parts[1:])
        actual = destination / relative
    product_root = actual / "lib" / PRODUCT
    return Layout(
        configured_prefix=configured,
        destdir=destination,
        actual_prefix=actual,
        product_root=product_root,
        version_root=product_root / version,
        current_link=product_root / "current",
        cli_launcher=actual / "bin" / PRODUCT,
        gui_launcher=actual / "bin" / f"{PRODUCT}-gui",
        desktop_entry=actual / "share" / "applications" / f"{PRODUCT}.desktop",
    )


def _require_native_host() -> None:
    machine = platform.machine().lower()
    if (platform.system() != "Linux" or machine not in {"x86_64", "amd64"}
            or platform.python_implementation() != "CPython"
            or sys.version_info[:2] != (3, 12)
            or sys.maxsize <= 2**32):
        raise InstallError(
            "make install requires 64-bit CPython 3.12 on Ubuntu/Linux x86-64")
    os_release = Path("/etc/os-release")
    try:
        release_values = {}
        for line in os_release.read_text(encoding="utf-8").splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            key, value = line.split("=", 1)
            release_values[key] = value.strip().strip('"')
    except (OSError, UnicodeError) as exc:
        raise InstallError(
            "make install supports Ubuntu 24.04 x86-64 and could not read "
            "/etc/os-release") from exc
    if (release_values.get("ID") != "ubuntu"
            or release_values.get("VERSION_ID") != "24.04"):
        raise InstallError(
            "make install supports Ubuntu 24.04 x86-64 only; "
            f"detected ID={release_values.get('ID', 'unknown')!r}, "
            f"VERSION_ID={release_values.get('VERSION_ID', 'unknown')!r}")


def _require_install_prerequisites() -> None:
    _require_native_host()
    for executable, package_name in (
        ("g++", "g++"), ("make", "make"), ("fc-match", "fontconfig"),
    ):
        if shutil.which(executable) is None:
            raise InstallError(
                f"required command {executable!r} is unavailable; "
                f"install the Ubuntu {package_name} package")
    try:
        import venv  # noqa: F401
    except ImportError as exc:
        raise InstallError(
            "Python venv support is required; install python3.12-venv") from exc
    try:
        import tkinter
        if tkinter.TkVersion < 8.6:
            raise ImportError(f"Tk {tkinter.TkVersion} is too old")
    except ImportError as exc:
        raise InstallError(
            "Python Tk 8.6 support is required; install python3-tk") from exc
    import sysconfig
    python_header = Path(sysconfig.get_path("include")) / "Python.h"
    if not python_header.is_file():
        raise InstallError(
            "Python development headers are required; install python3.12-dev")


def _require_gui_runtime() -> None:
    _require_native_host()
    if shutil.which("fc-match") is None:
        raise InstallError(
            "required command 'fc-match' is unavailable; install fontconfig")
    try:
        import tkinter
        if tkinter.TkVersion < 8.6:
            raise ImportError(f"Tk {tkinter.TkVersion} is too old")
    except ImportError as exc:
        raise InstallError(
            "Python Tk 8.6 support is required; install python3-tk") from exc


def _run(command: list[str], *, cwd: Path | None = None,
         input_bytes: bytes | None = None, env: dict[str, str] | None = None,
         capture: bool = False) -> subprocess.CompletedProcess[bytes]:
    print("+ " + " ".join(command), flush=True)
    completed = subprocess.run(
        command, cwd=cwd, input=input_bytes,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        env=env, check=False,
    )
    if completed.returncode != 0:
        detail = ""
        if capture:
            detail = (completed.stderr or completed.stdout).decode(
                "utf-8", errors="replace").strip()
        raise InstallError(
            f"command failed with exit {completed.returncode}: "
            f"{' '.join(command)}" + (f"\n{detail}" if detail else ""))
    return completed


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="ascii", newline="\n")


def _marker_payload(layout: Layout, version: str, kind: str) -> dict[str, object]:
    return {
        "schema": 1,
        "product": PRODUCT,
        "version": version,
        "configured_prefix": str(layout.configured_prefix),
        "kind": kind,
    }


def _valid_marker(root: Path, layout: Layout, version: str,
                  marker_name: str = INSTALL_MARKER,
                  kind: str = "installed-version") -> bool:
    marker = root / marker_name
    if root.is_symlink() or not marker.is_file() or marker.is_symlink():
        return False
    try:
        payload = json.loads(marker.read_text(encoding="ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return payload == _marker_payload(layout, version, kind)


def _safe_remove_tree(root: Path, layout: Layout, version: str, *, stage: bool) -> None:
    marker = STAGE_MARKER if stage else INSTALL_MARKER
    kind = "transaction-stage" if stage else "installed-version"
    if root.exists() or root.is_symlink():
        if not _valid_marker(root, layout, version, marker, kind):
            raise InstallError(f"refusing to remove unmarked directory: {root}")
        shutil.rmtree(root)


@contextmanager
def _install_lock(layout: Layout):
    layout.product_root.mkdir(parents=True, exist_ok=True)
    lock_path = layout.product_root / ".install.lock"
    handle = lock_path.open("a+b")
    try:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _managed_artifact_version(path: Path) -> str | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        first = path.read_text(encoding="utf-8")[:512]
    except (OSError, UnicodeError):
        return None
    if MANAGED_LINE not in first:
        return None
    match = re.search(r"^# version=([0-9]+(?:\.[0-9]+)+)$", first, re.MULTILINE)
    return match.group(1) if match is not None else None


def _managed_artifact(path: Path, version: str) -> bool:
    return _managed_artifact_version(path) == version


def _current_version(layout: Layout) -> str | None:
    link = layout.current_link
    if not link.is_symlink():
        return None
    target = os.readlink(link)
    if (Path(target).is_absolute() or "/" in target or "\\" in target
            or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", target)):
        return None
    target_root = layout.product_root / target
    return target if _valid_marker(target_root, layout, target) else None


def _check_artifact_targets(layout: Layout) -> None:
    for path in (layout.cli_launcher, layout.gui_launcher, layout.desktop_entry):
        if (path.exists() or path.is_symlink()) and _managed_artifact_version(path) is None:
            raise InstallError(f"refusing to overwrite foreign file: {path}")
    if layout.current_link.exists() or layout.current_link.is_symlink():
        if _current_version(layout) is None:
            raise InstallError(
                f"refusing to replace foreign activation link: {layout.current_link}")


def _atomic_file(path: Path, contents: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _atomic_current_link(layout: Layout, version: str) -> None:
    temporary = layout.product_root / f".current.{uuid.uuid4().hex}"
    try:
        os.symlink(version, temporary)
        os.replace(temporary, layout.current_link)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _launcher(version: str, *, gui: bool) -> str:
    command = (
        f'export MBUPRIME_ASSET_ROOT="$root/assets"\n'
        f'exec "$root/venv/bin/python" "$root/packaging/linux_gui_bootstrap.py" "$@"'
        if gui else 'exec "$root/venv/bin/python" -m mbuprime_structlab "$@"'
    )
    return f"""#!/bin/sh
{MANAGED_LINE}
# version={version}
set -eu
self="$(readlink -f -- "$0")"
prefix="$(CDPATH= cd -- "$(dirname -- "$self")/.." && pwd -P)"
root="$prefix/lib/{PRODUCT}/current"
test -x "$root/venv/bin/python" || {{ echo "MBUprime StructLab installation is incomplete: $root" >&2; exit 3; }}
{command}
"""


def _desktop_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("\"", "\\\"")
    escaped = escaped.replace("`", "\\`").replace("$", "\\$")
    return f'"{escaped}"'


def _desktop_entry(layout: Layout, version: str) -> str:
    prefix = layout.configured_prefix
    icon = prefix / "lib" / PRODUCT / "current" / "assets" / "mbu_sl_laboratory_tile.svg"
    return f"""[Desktop Entry]
{MANAGED_LINE}
# version={version}
Type=Application
Name=MBUprime StructLab
Comment=PCR and qPCR oligonucleotide structure analysis
Exec={_desktop_quote(str(prefix / 'bin' / f'{PRODUCT}-gui'))}
Icon={_desktop_quote(str(icon))}
Terminal=false
Categories=Science;Education;
StartupNotify=true
"""


def _pip_install(venv_python: Path, lock: Path, wheelhouse: Path | None) -> None:
    command = [
        str(venv_python), "-m", "pip", "--isolated", "install",
        "--disable-pip-version-check", "--require-hashes", "--only-binary=:all:",
    ]
    if wheelhouse is not None:
        command.extend(["--no-index", "--find-links", str(wheelhouse)])
    command.extend(["-r", str(lock)])
    _run(command)


def _prepare_stage(source_root: Path, layout: Layout, version: str,
                   stage: Path, wheelhouse: Path | None) -> None:
    stage.mkdir(mode=0o755)
    _write_json(stage / STAGE_MARKER, _marker_payload(
        layout, version, "transaction-stage"))
    venv_dir = stage / "venv"
    _run([sys.executable, "-m", "venv", str(venv_dir)])
    venv_python = venv_dir / "bin" / "python"
    _pip_install(venv_python, source_root / BUILD_LOCK, wheelhouse)
    _pip_install(venv_python, source_root / RUNTIME_LOCK, wheelhouse)

    wheel_dir = stage / ".wheel"
    wheel_dir.mkdir()
    build_env = dict(os.environ)
    build_env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    _run([
        str(venv_python), "-m", "pip", "--isolated", "wheel",
        "--disable-pip-version-check", "--no-build-isolation", "--no-deps",
        "--no-index", "--wheel-dir", str(wheel_dir), str(source_root),
    ], cwd=source_root, env=build_env)
    wheels = sorted(wheel_dir.glob(f"mbuprime_structlab-{version}-*.whl"))
    if len(wheels) != 1:
        raise InstallError(f"expected one local application wheel, found {len(wheels)}")
    _run([
        str(venv_python), "-m", "pip", "--isolated", "install",
        "--disable-pip-version-check", "--no-index", "--no-deps", str(wheels[0]),
    ])
    shutil.rmtree(wheel_dir)

    (stage / "packaging").mkdir()
    shutil.copy2(source_root / "packaging" / "linux_gui_bootstrap.py",
                 stage / "packaging" / "linux_gui_bootstrap.py")
    runtime_assets = (
        "mbu_sl_laboratory_tile.ico",
        "mbu_sl_laboratory_tile.svg",
        "fonts/CascadiaMono.ttf",
        "fonts/OFL.txt",
        "fonts/SOURCE.txt",
        "fonts/linux-fonts.conf",
    )
    for relative in runtime_assets:
        source = source_root / "assets" / relative
        if not source.is_file():
            raise InstallError(f"required GUI runtime asset is missing: assets/{relative}")
        destination = stage / "assets" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    for name in ("COPYING", "README.md", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(source_root / name, stage / name)
    corresponding = stage / "rnastructure-corresponding-source"
    corresponding.mkdir()
    shutil.copy2(source_root / "vendor" / "rnastructure-6.6" /
                 "RNAstructureSource.zip", corresponding / "RNAstructureSource.zip")

    _validate_runtime(venv_python)
    # Write the installed marker before removing the transaction marker so
    # recovery can recognize completed or in-progress stages.
    _write_json(stage / INSTALL_MARKER, _marker_payload(
        layout, version, "installed-version"))
    (stage / STAGE_MARKER).unlink()


def _validation_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop("DISPLAY", None)
    environment.pop("PYTHONPATH", None)
    environment.pop("WAYLAND_DISPLAY", None)
    environment["MPLBACKEND"] = "Agg"
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def _validate_runtime(venv_python: Path) -> None:
    probe = (
        "import external_engines as e,primer3,vienna_backend as v;"
        "assert getattr(primer3,'__version__','')=='2.3.0';"
        "assert v.version()=='2.7.2';"
        "i=e.require_mandatory_engines();"
        "assert i['RNAstructure']['engine_version']=='6.6';"
        "assert i['seqfold']['version']=='0.10.2'"
    )
    environment = _validation_environment()
    # A neutral working directory plus safe-path mode prevents the source
    # checkout from shadowing the just-installed wheel during validation.
    with tempfile.TemporaryDirectory(prefix="mbuprime-installed-probe-") as raw:
        neutral_cwd = Path(raw)
        _run(
            [str(venv_python), "-P", "-c", probe], cwd=neutral_cwd,
            env=environment, capture=True)
        completed = _run([
            str(venv_python), "-P", "-m", "mbuprime_structlab", "analyze", "-",
            "--format", "tsv", "--progress", "none", "--no-additional-analysis",
        ], cwd=neutral_cwd, input_bytes=LIVE_INPUT, env=environment, capture=True)
    if b"install_check" not in completed.stdout:
        raise InstallError("live headless CLI validation did not produce a report")


def _artifact_snapshot(paths: tuple[Path, ...]) -> dict[Path, tuple[bytes, int] | None]:
    result: dict[Path, tuple[bytes, int] | None] = {}
    for path in paths:
        result[path] = ((path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                        if path.is_file() and not path.is_symlink() else None)
    return result


def _restore_artifacts(snapshot: dict[Path, tuple[bytes, int] | None]) -> None:
    for path, previous in snapshot.items():
        if previous is None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        else:
            data, mode = previous
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{path.name}.rollback.", dir=path.parent)
            temporary_path = Path(temporary)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temporary_path, mode)
                os.replace(temporary_path, path)
            finally:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass


def _restore_current_link(layout: Layout, previous: str | None) -> None:
    try:
        layout.current_link.unlink()
    except FileNotFoundError:
        pass
    if previous is not None:
        _atomic_current_link(layout, previous)


def _recover_interrupted_reinstall(layout: Layout, version: str) -> None:
    """Restore or clean only marker-validated remnants of this version."""

    backups = sorted(layout.product_root.glob(f".{version}.rollback-*"))
    stages = sorted(layout.product_root.glob(f".{version}.install-*"))
    for backup in backups:
        if not _valid_marker(backup, layout, version):
            raise InstallError(
                f"refusing automatic recovery of unmarked backup: {backup}")
    version_present = (
        layout.version_root.exists() or layout.version_root.is_symlink())
    if not version_present and backups:
        if len(backups) != 1:
            raise InstallError(
                "cannot recover interrupted reinstall with multiple backups: "
                + ", ".join(str(path) for path in backups))
        os.replace(backups[0], layout.version_root)
        backups = []
        version_present = True
    if version_present and _valid_marker(layout.version_root, layout, version):
        for backup in backups:
            _safe_remove_tree(backup, layout, version, stage=False)
    for stage in stages:
        if _valid_marker(
                stage, layout, version, STAGE_MARKER, "transaction-stage"):
            _safe_remove_tree(stage, layout, version, stage=True)
        elif _valid_marker(stage, layout, version):
            _safe_remove_tree(stage, layout, version, stage=False)
        else:
            raise InstallError(
                f"refusing automatic recovery of unmarked stage: {stage}")


@dataclass(frozen=True)
class _InstallTransaction:
    source_root: Path
    layout: Layout
    version: str
    wheelhouse: Path | None
    stage: Path
    backup: Path
    artifacts: tuple[Path, ...]


@dataclass(frozen=True)
class _InstallRollback:
    artifact_snapshot: dict[Path, tuple[bytes, int] | None]
    previous_current: str | None
    had_previous: bool


def _validated_install_transaction(
    source_root: Path, layout: Layout, version: str,
    wheelhouse: Path | None,
) -> _InstallTransaction:
    """Validate immutable install inputs and allocate transaction paths."""

    _require_install_prerequisites()
    for relative in (BUILD_LOCK, RUNTIME_LOCK, "pyproject.toml", "setup.py",
                     "packaging/linux_gui_bootstrap.py"):
        if not (source_root / relative).is_file():
            raise InstallError(f"required source-install input is missing: {relative}")
    if wheelhouse is not None and not wheelhouse.is_dir():
        raise InstallError(f"WHEELHOUSE is not a directory: {wheelhouse}")
    layout.product_root.mkdir(parents=True, exist_ok=True)
    return _InstallTransaction(
        source_root=source_root,
        layout=layout,
        version=version,
        wheelhouse=wheelhouse,
        stage=layout.product_root / f".{version}.install-{uuid.uuid4().hex}",
        backup=layout.product_root / f".{version}.rollback-{uuid.uuid4().hex}",
        artifacts=(layout.cli_launcher, layout.gui_launcher,
                   layout.desktop_entry),
    )


def _rollback_install_commit(
    transaction: _InstallTransaction, rollback: _InstallRollback,
) -> None:
    """Restore the pre-commit installation and managed artifacts in order."""

    layout = transaction.layout
    if layout.version_root.exists():
        _safe_remove_tree(
            layout.version_root, layout, transaction.version, stage=False)
    if rollback.had_previous and transaction.backup.exists():
        os.replace(transaction.backup, layout.version_root)
    _restore_current_link(layout, rollback.previous_current)
    _restore_artifacts(rollback.artifact_snapshot)


def _commit_install_transaction(transaction: _InstallTransaction) -> None:
    """Prepare, publish, and retire a single versioned installation."""

    layout = transaction.layout
    version = transaction.version
    _prepare_stage(
        transaction.source_root, layout, version, transaction.stage,
        transaction.wheelhouse)
    rollback = _InstallRollback(
        artifact_snapshot=_artifact_snapshot(transaction.artifacts),
        previous_current=(
            os.readlink(layout.current_link)
            if layout.current_link.is_symlink() else None),
        had_previous=layout.version_root.exists(),
    )
    if rollback.had_previous:
        os.replace(layout.version_root, transaction.backup)
    try:
        os.replace(transaction.stage, layout.version_root)
        _check_artifact_targets(layout)
        _atomic_current_link(layout, version)
        _atomic_file(layout.cli_launcher, _launcher(version, gui=False), 0o755)
        _atomic_file(layout.gui_launcher, _launcher(version, gui=True), 0o755)
        _atomic_file(layout.desktop_entry, _desktop_entry(layout, version), 0o644)
    except BaseException:
        _rollback_install_commit(transaction, rollback)
        raise
    if transaction.backup.exists():
        _safe_remove_tree(
            transaction.backup, layout, version, stage=False)


def _cleanup_failed_install_stage(transaction: _InstallTransaction) -> None:
    """Remove only a recognized transaction/install stage after failure."""

    stage = transaction.stage
    layout = transaction.layout
    version = transaction.version
    if not stage.exists():
        return
    stage_is_transaction = _valid_marker(
        stage, layout, version, STAGE_MARKER, "transaction-stage")
    stage_is_install = _valid_marker(stage, layout, version)
    if stage_is_transaction:
        _safe_remove_tree(stage, layout, version, stage=True)
    elif stage_is_install:
        _safe_remove_tree(stage, layout, version, stage=False)
    else:
        raise InstallError(
            f"failed transaction left an unrecognized stage: {stage}")


def install(source_root: Path, layout: Layout, version: str,
            wheelhouse: Path | None) -> None:
    transaction = _validated_install_transaction(
        source_root, layout, version, wheelhouse)
    with _install_lock(layout):
        _recover_interrupted_reinstall(layout, version)
        _check_artifact_targets(layout)
        version_path_present = (
            layout.version_root.exists() or layout.version_root.is_symlink())
        if version_path_present and not _valid_marker(
                layout.version_root, layout, version):
            raise InstallError(
                f"refusing to replace unmarked installation: {layout.version_root}")
        try:
            _commit_install_transaction(transaction)
        except BaseException:
            _cleanup_failed_install_stage(transaction)
            raise
    print(f"Installed MBUprime StructLab {version} under {layout.actual_prefix}")


def verify(layout: Layout, version: str) -> None:
    _require_native_host()
    if not _valid_marker(layout.version_root, layout, version):
        raise InstallError(f"validated installation marker is absent: {layout.version_root}")
    if _current_version(layout) != version:
        raise InstallError(f"active version link does not select {version}: {layout.current_link}")
    if not _managed_artifact(layout.cli_launcher, version):
        raise InstallError(f"managed CLI launcher is absent: {layout.cli_launcher}")
    if not _managed_artifact(layout.gui_launcher, version):
        raise InstallError(f"managed GUI launcher is absent: {layout.gui_launcher}")
    if not _managed_artifact(layout.desktop_entry, version):
        raise InstallError(f"managed desktop entry is absent: {layout.desktop_entry}")
    completed = _run([
        str(layout.cli_launcher), "analyze", "-", "--format", "tsv",
        "--progress", "none", "--no-additional-analysis",
    ], input_bytes=LIVE_INPUT, env=_validation_environment(), capture=True)
    if b"install_check" not in completed.stdout:
        raise InstallError("installed CLI verification did not produce a report")
    print(f"Verified installed CLI and mandatory engines: {layout.cli_launcher}")


def verify_gui(layout: Layout, version: str) -> None:
    _require_gui_runtime()
    if not _valid_marker(layout.version_root, layout, version):
        raise InstallError(f"validated installation marker is absent: {layout.version_root}")
    if _current_version(layout) != version:
        raise InstallError(f"active version link does not select {version}: {layout.current_link}")
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        raise InstallError("GUI verification requires DISPLAY or WAYLAND_DISPLAY (use xvfb-run)")
    with tempfile.TemporaryDirectory(prefix="mbuprime-gui-check-") as temporary:
        log = Path(temporary) / "gui-self-test.log"
        _run([str(layout.gui_launcher), "--self-test", "--self-test-log", str(log)])
        text = log.read_text(encoding="utf-8")
        if "Self-test OK:" not in text:
            raise InstallError("installed GUI self-test did not record success")
    print(f"Verified installed Tk GUI: {layout.gui_launcher}")


def _unlink_managed(path: Path, version: str) -> None:
    if not path.exists():
        return
    if not _managed_artifact(path, version):
        raise InstallError(f"refusing to remove foreign file: {path}")
    path.unlink()


def _rmdir_if_empty(path: Path) -> None:
    try:
        path.rmdir()
    except (FileNotFoundError, OSError):
        pass


def _deactivate_installed_version(
        layout: Layout, version: str, active_version: str | None) -> bool:
    if not (layout.current_link.exists() or layout.current_link.is_symlink()):
        return False
    if active_version is None:
        raise InstallError(
            f"refusing to remove foreign activation link: {layout.current_link}")
    if active_version != version:
        return False
    for artifact in (
            layout.cli_launcher, layout.gui_launcher, layout.desktop_entry):
        if not _managed_artifact(artifact, version):
            raise InstallError(
                f"refusing partial uninstall with foreign or missing "
                f"active artifact: {artifact}")
    layout.current_link.unlink()
    return True


def _remove_installed_version_tree(layout: Layout, version: str) -> None:
    if layout.version_root.exists() or layout.version_root.is_symlink():
        _safe_remove_tree(layout.version_root, layout, version, stage=False)


def _remove_activation_artifacts(layout: Layout, version: str) -> None:
    _unlink_managed(layout.cli_launcher, version)
    _unlink_managed(layout.gui_launcher, version)
    _unlink_managed(layout.desktop_entry, version)


def uninstall(layout: Layout, version: str) -> None:
    _require_native_host()
    with _install_lock(layout):
        active_version = _current_version(layout)
        deactivated = _deactivate_installed_version(
            layout, version, active_version)
        _remove_installed_version_tree(layout, version)
        if deactivated:
            _remove_activation_artifacts(layout, version)
    _rmdir_if_empty(layout.desktop_entry.parent)
    _rmdir_if_empty(layout.desktop_entry.parent.parent)
    _rmdir_if_empty(layout.cli_launcher.parent)
    # The lock file is deliberately retained as a non-executable ownership and
    # serialization sentinel; no parent or prefix is recursively removed.
    print(f"Uninstalled MBUprime StructLab {version} from {layout.actual_prefix}")


def print_layout(layout: Layout, version: str) -> None:
    values = {
        "version": version,
        "configured_prefix": layout.configured_prefix,
        "destdir": layout.destdir or "",
        "actual_prefix": layout.actual_prefix,
        "version_root": layout.version_root,
        "current_link": layout.current_link,
        "cli_launcher": layout.cli_launcher,
        "gui_launcher": layout.gui_launcher,
        "desktop_entry": layout.desktop_entry,
    }
    for key, value in values.items():
        print(f"{key}={value}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("install", "verify", "verify-gui", "print-layout", "uninstall"))
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--destdir", default="")
    parser.add_argument("--wheelhouse", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    source_root = args.source_root.resolve()
    try:
        version = _source_version(source_root)
        layout = layout_for(args.prefix, args.destdir, version)
        wheelhouse = (
            _absolute_path(str(args.wheelhouse), "WHEELHOUSE")
            if args.wheelhouse else None)
        if args.command == "install":
            install(source_root, layout, version, wheelhouse)
        elif args.command == "verify":
            verify(layout, version)
        elif args.command == "verify-gui":
            verify_gui(layout, version)
        elif args.command == "uninstall":
            uninstall(layout, version)
        else:
            print_layout(layout, version)
    except (InstallError, OSError) as exc:
        print(f"install error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("install error: interrupted", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
