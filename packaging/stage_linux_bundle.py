"""Stage Linux provenance and complete corresponding source before PyInstaller."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import sys


ROOT = Path(__file__).resolve().parents[1]
STAGING = ROOT / "build" / "linux-package-staging"
SOURCE = STAGING / "rnastructure-corresponding-source"
TARGET = "cp312-linux-x86_64"
ARCHIVE_SHA256 = "4e30fa06f10a89556ad070c8d141fff6090165c330df786e19f9627df4407fd4"
INVENTORY_PATH = ROOT / "packaging" / "release_inventory.json"


def _source_target(name: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    inventory = json.loads(INVENTORY_PATH.read_text(encoding="ascii"))
    target = inventory["targets"][name]
    files: set[str] = set()
    trees: set[str] = set()
    for group_name in target["groups"]:
        group = inventory["source_groups"][group_name]
        files.update(group.get("files", ()))
        trees.update(group.get("trees", ()))
    for artifact_name in target.get("generated_artifacts", ()):
        files.add(inventory["generated_artifacts"][artifact_name]["path"])
    return tuple(sorted(files)), tuple(sorted(trees))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stage_source() -> None:
    if STAGING.exists():
        resolved = STAGING.resolve()
        expected_parent = (ROOT / "build").resolve()
        if resolved.parent != expected_parent:
            raise RuntimeError(f"refusing to replace unexpected staging path: {resolved}")
        shutil.rmtree(resolved)
    SOURCE.mkdir(parents=True)
    source_files, source_trees = _source_target("linux_corresponding_source")
    for relative in source_files:
        source = ROOT / relative
        if not source.is_file():
            raise RuntimeError(f"required corresponding source is absent: {relative}")
        destination = SOURCE / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    for relative in source_trees:
        source = ROOT / relative
        if not source.is_dir():
            raise RuntimeError(f"required corresponding source tree is absent: {relative}")
        shutil.copytree(source, SOURCE / relative)

    files = {
        path.relative_to(SOURCE).as_posix(): _sha256(path)
        for path in sorted(SOURCE.rglob("*")) if path.is_file()
    }
    (SOURCE / "source-manifest.json").write_text(
        json.dumps(files, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="ascii", newline="\n",
    )


def _version(distribution: str) -> str:
    return importlib.metadata.version(distribution)


def _build_utc() -> str:
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    value = (
        datetime.fromtimestamp(int(epoch), timezone.utc)
        if epoch is not None else datetime.now(timezone.utc)
    )
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_provenance() -> None:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import scientific_metadata

    manifest_path = (
        ROOT / "rnastructure_native" / "native_manifests" / f"{TARGET}.json")
    if not manifest_path.is_file():
        raise RuntimeError(
            "target RNAstructure manifest is absent; build the native extension "
            "in place on Ubuntu x86-64 first")
    native = json.loads(manifest_path.read_text(encoding="ascii"))
    archive = ROOT / "vendor" / "rnastructure-6.6" / "RNAstructureSource.zip"
    if not archive.is_file() or _sha256(archive) != ARCHIVE_SHA256:
        raise RuntimeError("vendored RNAstructure source archive hash mismatch")
    runtime_lock = ROOT / "requirements-targets" / "ubuntu-24.04-x86_64.txt"
    primer3_identity = scientific_metadata.validated_primer3_runtime_identity()
    vienna_identity = scientific_metadata.validated_vienna_runtime_identity()
    payload = {
        "schema_version": scientific_metadata.MANIFEST_SCHEMA_VERSION,
        "application_version": scientific_metadata.APPLICATION_VERSION,
        "scientific_policy_version": scientific_metadata.SCIENTIFIC_POLICY_VERSION,
        "build_utc": _build_utc(),
        "source_tree_sha256": scientific_metadata.compute_source_tree_hash(ROOT),
        "requirements_lock_sha256": _sha256(runtime_lock),
        "runtime": {
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "operating_system": platform.system(),
            "architecture": platform.machine(),
            "python_architecture": platform.architecture()[0],
        },
        "dependencies": {
            "primer3_py_version": primer3_identity["primer3_py_version"],
            "underlying_primer3_version": primer3_identity["libprimer3_version"],
            "primer3_runtime_identity": primer3_identity,
            "viennarna_version": vienna_identity["version"],
            "viennarna_runtime_identity": vienna_identity,
            "seqfold_version": _version("seqfold"),
            "vienna_parameter_set": scientific_metadata.VIENNA_PARAMETER_SET,
            "matplotlib_version": _version("matplotlib"),
            "pyinstaller_version": _version("pyinstaller"),
        },
        "rnastructure_native": {
            key: native[key] for key in (
                "integration", "engine_version", "target", "abi_tag", "compiler",
                "native_module_sha256", "upstream_source_archive_sha256",
                "upstream_source_manifest_sha256", "source_manifest_sha256",
                "dna_table_manifest_sha256",
                "compatibility_patch",
            )
        },
        "rnastructure_native_manifest_sha256": _sha256(manifest_path),
    }
    (STAGING / "release_provenance.json").write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="ascii", newline="\n",
    )


def main() -> int:
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise RuntimeError("Linux package staging requires a native x86-64 host")
    _stage_source()
    _write_provenance()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
