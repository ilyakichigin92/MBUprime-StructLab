"""Verify the native Ubuntu GUI+CLI onedir tarball fail closed."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import tempfile


EXPECTED_ROOT = "mbuprime-structlab-linux-x86_64"
GUI_NAME = "MBUprime StructLab"
CLI_NAME = "mbuprime-structlab"
SOURCE_ARCHIVE_SHA256 = (
    "4e30fa06f10a89556ad070c8d141fff6090165c330df786e19f9627df4407fd4")
CLI_INPUT = b"name\trole\tsequence\ncontrol\tprimer\tACGTACGTACGTACGTACGT\n"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_members(members: list[tarfile.TarInfo]) -> None:
    for member in members:
        path = PurePosixPath(member.name)
        if not path.parts or path.is_absolute() or ".." in path.parts:
            raise RuntimeError(f"unsafe tar member: {member.name}")
        if path.parts[0] != EXPECTED_ROOT:
            raise RuntimeError(f"tar member escapes the single onedir root: {member.name}")
        if member.issym() or member.islnk():
            target = PurePosixPath(member.linkname)
            if target.is_absolute() or ".." in target.parts:
                raise RuntimeError(f"unsafe tar link: {member.name} -> {member.linkname}")


def _require_elf(path: Path) -> None:
    if not path.is_file():
        raise RuntimeError(f"launcher is absent: {path.name}")
    magic = path.read_bytes()[:4]
    if magic[:2] == b"MZ":
        raise RuntimeError(f"Windows PE launcher leaked into Linux package: {path.name}")
    if magic != b"\x7fELF":
        raise RuntimeError(f"launcher is not ELF: {path.name}")
    if not os.access(path, os.X_OK):
        raise RuntimeError(f"launcher is not executable: {path.name}")


def _verify_corresponding_source(internal: Path) -> None:
    source = internal / "rnastructure-corresponding-source"
    archive = source / "vendor" / "rnastructure-6.6" / "RNAstructureSource.zip"
    if _sha256(archive) != SOURCE_ARCHIVE_SHA256:
        raise RuntimeError("packaged RNAstructureSource.zip hash mismatch")
    manifest_path = source / "source-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="ascii"))
    if not isinstance(manifest, dict) or not manifest:
        raise RuntimeError("corresponding-source manifest is empty or invalid")
    expected = {
        path.relative_to(source).as_posix(): _sha256(path)
        for path in source.rglob("*")
        if path.is_file() and path != manifest_path
    }
    if manifest != expected:
        raise RuntimeError("corresponding-source manifest does not match payload")
    inventory = json.loads(
        (source / "packaging" / "release_inventory.json").read_text(
            encoding="ascii"))
    target = inventory["targets"]["linux_corresponding_source"]
    required: set[str] = set()
    trees: set[str] = set()
    for group_name in target["groups"]:
        group = inventory["source_groups"][group_name]
        required.update(group.get("files", ()))
        trees.update(group.get("trees", ()))
    for artifact_name in target.get("generated_artifacts", ()):
        required.add(inventory["generated_artifacts"][artifact_name]["path"])
    missing = required - set(manifest)
    unexpected = {
        path for path in manifest
        if path not in required
        and not any(path.startswith(f"{tree}/") for tree in trees)
    }
    if missing or unexpected:
        raise RuntimeError(
            "corresponding-source members differ from canonical Linux target: "
            f"missing={sorted(missing)}, unexpected={sorted(unexpected)}")


def _verify_cli(cli: Path) -> None:
    env = os.environ.copy()
    env.pop("DISPLAY", None)
    completed = subprocess.run(
        [str(cli), "analyze", "-", "--format", "tsv", "--progress", "none",
         "--no-additional-analysis"],
        input=CLI_INPUT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, check=False, timeout=180,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"packaged CLI analysis exited {completed.returncode}: "
            f"{completed.stderr.decode('utf-8', errors='replace')}")
    if completed.stderr:
        raise RuntimeError("packaged CLI --progress none wrote to stderr")
    if not completed.stdout or b"\r" in completed.stdout:
        raise RuntimeError("packaged CLI output is empty or not LF canonical")
    first_line = completed.stdout.splitlines()[0]
    if b"\t" not in first_line or b"record" not in first_line.lower():
        raise RuntimeError("packaged CLI output lacks the canonical TSV header")


def _verify_gui(gui: Path, work: Path) -> None:
    xvfb = shutil.which("xvfb-run")
    if xvfb is None:
        raise RuntimeError("xvfb-run is required for the packaged GUI self-test")
    log = work / "linux-gui-self-test.log"
    completed = subprocess.run(
        [xvfb, "-a", str(gui), "--self-test", "--self-test-log", str(log)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=240,
    )
    if completed.returncode != 0:
        details = completed.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"packaged GUI self-test failed: {details}")
    text = log.read_text(encoding="utf-8")
    for required in (
        "stage=tk-root-ready", "stage=font-register status=ok",
        "stage=gui-import status=ok", "stage=primer3 status=ok",
        "stage=vienna status=ok", "stage=rnastructure status=ok",
        "stage=seqfold status=ok", "Self-test OK:",
    ):
        if required not in text:
            raise RuntimeError(f"packaged GUI self-test omitted {required!r}")


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: verify_linux_cli_tar.py ARCHIVE.tar.gz")
    archive = Path(sys.argv[1]).resolve()
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        _validate_members(members)
        with tempfile.TemporaryDirectory(prefix="mbuprime-linux-verify-") as folder:
            bundle.extractall(folder, filter="data")
            root = Path(folder) / EXPECTED_ROOT
            root_names = {path.name for path in root.iterdir()}
            if root_names != {"_internal", GUI_NAME, CLI_NAME}:
                raise RuntimeError(f"unexpected onedir root topology: {sorted(root_names)}")
            internal = root / "_internal"
            if not internal.is_dir():
                raise RuntimeError("shared _internal directory is absent")
            gui = root / GUI_NAME
            cli = root / CLI_NAME
            _require_elf(gui)
            _require_elf(cli)
            _verify_corresponding_source(internal)
            _verify_cli(cli)
            _verify_gui(gui, Path(folder))
    print(_sha256(archive), archive.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
