"""Generate a target-qualified RNAstructure native integrity manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import sysconfig

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from native_target import current_target


PACKAGE = ROOT / "rnastructure_native"
SOURCE_ARCHIVE = ROOT / "vendor" / "rnastructure-6.6" / "RNAstructureSource.zip"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_manifest(paths: list[Path], base: Path) -> dict[str, str]:
    return {
        path.relative_to(base).as_posix(): _sha256(path)
        for path in sorted(paths)
    }


def _manifest_hash(payload: dict[str, str]) -> str:
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _native_module(package: Path) -> Path:
    extension_suffix = str(sysconfig.get_config_var("EXT_SUFFIX") or "")
    if not extension_suffix:
        raise RuntimeError("CPython did not report an extension suffix")
    modules = sorted(package.glob(f"_rnastructure_native_v1*{extension_suffix}"))
    if len(modules) != 1:
        raise RuntimeError(
            f"expected one native module for {extension_suffix}, found {len(modules)}")
    return modules[0]


def generate_manifest(
    package: Path = PACKAGE, *, compiler: str = "unknown",
) -> Path:
    """Hash a native-host build and return the written manifest path."""

    target = current_target()
    module = _native_module(package)
    source_files = [
        ROOT / "native" / "rnastructure_native.cpp",
        ROOT / "native" / "setup_native.py",
        ROOT / "native" / "build_native.ps1",
        ROOT / "native" / "generate_native_manifest.py",
        ROOT / "rnastructure_native" / "__init__.py",
        ROOT / "native_target.py",
    ]
    if not all(path.is_file() for path in source_files):
        raise RuntimeError("RNAstructure native source manifest is incomplete")
    source_manifest = _tree_manifest(source_files, ROOT)
    from native.setup_native import verified_upstream_source_manifest

    upstream_source_manifest = verified_upstream_source_manifest()
    table_files = [
        path for path in (package / "data_tables").rglob("*") if path.is_file()
    ]
    if not table_files:
        raise RuntimeError("RNAstructure DNA tables are unavailable")
    table_manifest = _tree_manifest(table_files, package)
    expected_archive_hash = (
        "4e30fa06f10a89556ad070c8d141fff6090165c330df786e19f9627df4407fd4")
    if not SOURCE_ARCHIVE.is_file() or _sha256(SOURCE_ARCHIVE) != expected_archive_hash:
        raise RuntimeError("vendored RNAstructureSource.zip hash mismatch")
    extension_suffix = str(sysconfig.get_config_var("EXT_SUFFIX") or "")
    fallback_abi = extension_suffix.removeprefix(".")
    for suffix in (".pyd", ".so"):
        fallback_abi = fallback_abi.removesuffix(suffix)
    payload = {
        "schema_version": 3,
        "integration": "in_process_native",
        "engine_version": "6.6",
        "target": target,
        "abi_tag": sysconfig.get_config_var("SOABI") or fallback_abi,
        "compiler": compiler,
        "native_module": module.name,
        "native_module_sha256": _sha256(module),
        "upstream_source_archive": SOURCE_ARCHIVE.relative_to(ROOT).as_posix(),
        "upstream_source_archive_sha256": _sha256(SOURCE_ARCHIVE),
        "upstream_source_manifest_sha256": _manifest_hash(
            upstream_source_manifest),
        "source_manifest_sha256": _manifest_hash(source_manifest),
        "dna_table_manifest_sha256": _manifest_hash(table_manifest),
        "compatibility_patch": "rnastructure_6_6_dna_temperature_tables_v2",
        "source_files": source_manifest,
        "upstream_source_files": upstream_source_manifest,
        "dna_table_files": table_manifest,
    }
    destination = package / "native_manifests" / f"{target}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="ascii",
    )
    return destination


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, default=PACKAGE)
    parser.add_argument("--compiler", default="unknown")
    args = parser.parse_args()
    generate_manifest(args.package.resolve(), compiler=args.compiler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
