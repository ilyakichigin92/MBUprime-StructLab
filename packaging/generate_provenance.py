"""Create canonical provenance before PyInstaller packages the app.

Canonical JSON and hashes identify these inputs; they do not establish that
separate compiler/PyInstaller runs produce byte-identical executable files.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = Path(__file__).with_name("release_provenance.json")
INVENTORY_PATH = Path(__file__).with_name("release_inventory.json")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _source_tree_hash(
    project_root: Path,
    overrides: dict[str, Path] | None = None,
) -> str:
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    module = importlib.import_module("scientific_metadata")
    return str(module.compute_source_tree_hash(
        project_root, overrides=overrides))


def _scientific_constants() -> dict[str, str]:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    module = importlib.import_module("scientific_metadata")
    try:
        return {
            "application_version": str(module.APPLICATION_VERSION),
            "scientific_policy_version": str(module.SCIENTIFIC_POLICY_VERSION),
            "schema_version": str(module.MANIFEST_SCHEMA_VERSION),
            "vienna_parameter_set": str(module.VIENNA_PARAMETER_SET),
        }
    except AttributeError as error:
        raise ValueError(f"Required scientific metadata constant missing: {error.name}") from error


def _installed_version(distribution: str) -> str | None:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None


def _validated_engine_versions() -> dict[str, object]:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    scientific = importlib.import_module("scientific_metadata")
    primer3 = importlib.import_module("primer3")
    primer3_identity = scientific.validated_primer3_runtime_identity(primer3)
    vienna_identity = scientific.validated_vienna_runtime_identity()
    return {
        **primer3_identity,
        "viennarna_version": vienna_identity["version"],
        "primer3_runtime_identity": primer3_identity,
        "viennarna_runtime_identity": vienna_identity,
    }


def _source_commit(project_root: Path) -> str:
    head = project_root / ".git" / "HEAD"
    try:
        value = head.read_text(encoding="utf-8").strip()
        if value.startswith("ref: "):
            value = (project_root / ".git" / value[5:]).read_text(
                encoding="utf-8").strip()
        return value
    except OSError:
        return ""


def build_source_provenance(
    project_root: Path,
    overrides: dict[str, Path] | None = None,
) -> dict[str, str]:
    """Return the immutable scientific identity embedded in source wheels."""

    lock_path = project_root / "requirements-lock.txt"
    return {
        "source_tree_sha256": _source_tree_hash(project_root, overrides),
        "requirements_lock_sha256": _sha256_file(lock_path),
        "source_commit": _source_commit(project_root),
    }


def build_provenance(project_root: Path, build_utc: str) -> dict[str, Any]:
    constants = _scientific_constants()
    engines = _validated_engine_versions()
    source_provenance = build_source_provenance(project_root)
    inventory = json.loads(INVENTORY_PATH.read_text(encoding="ascii"))
    generated_name = inventory["targets"]["scientific_identity"][
        "generated_artifacts_by_target"]["cp312-windows-x86_64"]
    native_manifest_path = project_root / inventory["generated_artifacts"][
        generated_name]["path"]
    native_manifest = json.loads(native_manifest_path.read_text(encoding="ascii"))
    return {
        "schema_version": constants["schema_version"],
        "application_version": constants["application_version"],
        "scientific_policy_version": constants["scientific_policy_version"],
        "build_utc": build_utc,
        **source_provenance,
        "runtime": {
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "operating_system": platform.system(),
            "architecture": platform.machine(),
            "python_architecture": platform.architecture()[0],
        },
        "dependencies": {
            "primer3_py_version": engines["primer3_py_version"],
            "underlying_primer3_version": engines["libprimer3_version"],
            "primer3_runtime_identity": engines["primer3_runtime_identity"],
            "viennarna_version": engines["viennarna_version"],
            "viennarna_runtime_identity": engines["viennarna_runtime_identity"],
            "seqfold_version": _installed_version("seqfold"),
            "vienna_parameter_set": constants["vienna_parameter_set"],
            "matplotlib_version": _installed_version("matplotlib"),
            "pyinstaller_version": _installed_version("pyinstaller"),
        },
        "rnastructure_native": {
            key: native_manifest[key] for key in (
                "integration", "engine_version", "target", "abi_tag", "compiler",
                "native_module_sha256", "upstream_source_archive_sha256",
                "upstream_source_manifest_sha256", "source_manifest_sha256",
                "dna_table_manifest_sha256",
                "compatibility_patch",
            )
        },
        "rnastructure_native_manifest_sha256": _sha256_file(
            native_manifest_path),
    }


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"


def _build_utc() -> str:
    source_date_epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if source_date_epoch is not None:
        return datetime.fromtimestamp(int(source_date_epoch), timezone.utc).isoformat().replace("+00:00", "Z")
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--build-utc", default=None, help="ISO-8601 timestamp for deterministic tests")
    args = parser.parse_args(argv)
    payload = build_provenance(PROJECT_ROOT, args.build_utc or _build_utc())
    args.output.write_text(canonical_json(payload), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
