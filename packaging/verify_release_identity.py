"""Fail closed when source, provenance, signing, and packaged identities diverge."""

from __future__ import annotations

import argparse
from collections import namedtuple
from contextlib import contextmanager
import fnmatch
import hashlib
import importlib.util
import json
import marshal
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from types import CodeType
from typing import Iterator
import zipfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROVENANCE_PATH = PROJECT_ROOT / "packaging" / "release_provenance.json"
INVENTORY_RELATIVE_PATH = Path("packaging/release_inventory.json")
APPLICATION_DIR_NAME = "MBUprime StructLab"
EXECUTABLE_NAME = "MBUprime StructLab.exe"
SIGNING_ATTESTATION_NAME = "MBUprime StructLab.signing-attestation.json"
EXECUTABLE_SIDECAR_NAME = f"{EXECUTABLE_NAME}.sha256"
RELEASE_ROOT_ENTRIES = frozenset({EXECUTABLE_NAME, "_internal"})
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from scientific_metadata import (
    MANIFEST_SCHEMA_VERSION as REQUIRED_MANIFEST_SCHEMA_VERSION,
    SCIENTIFIC_POLICY_VERSION as REQUIRED_SCIENTIFIC_POLICY_VERSION,
)
TRUST_CLASSIFICATIONS = {
    "unsigned": "unsigned_development",
    "signed": "signed_authenticode_valid_trusted_on_verification_host",
}
REQUIRED_APP_FILES = (
    "_internal/COPYING",
    "_internal/CORRESPONDING_SOURCE.md",
    "_internal/README.md",
    f"_internal/{EXECUTABLE_SIDECAR_NAME}",
    f"_internal/{SIGNING_ATTESTATION_NAME}",
    "_internal/THIRD_PARTY_NOTICES.md",
    "_internal/LICENSE-MANIFEST.json",
    "_internal/SBOM.spdx.json",
    "_internal/assets/fonts/CascadiaMono.ttf",
    "_internal/assets/fonts/OFL.txt",
    "_internal/assets/fonts/SOURCE.txt",
    "_internal/assets/mbu_sl_laboratory_tile.ico",
    "_internal/assets/mbu_sl_laboratory_tile.svg",
    "_internal/release_provenance.json",
    "_internal/rnastructure-corresponding-source/source-manifest.json",
)
REQUIRED_SELF_TEST_STAGES = frozenset({
    "tk", "font", "matplotlib", "exports", "primer3", "vienna",
    "rnastructure", "seqfold", "archive",
})


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _zip_member_sha256(archive: zipfile.ZipFile, name: str) -> str:
    digest = hashlib.sha256()
    with archive.open(name) as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return payload


def _release_evidence_verifier(project_root: Path):
    path = project_root / "packaging" / "generate_release_evidence.py"
    spec = importlib.util.spec_from_file_location("mbuprime_release_evidence", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load release evidence verifier: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.verify_release_evidence


def _diagnostic_value(value: object) -> str:
    """Describe mismatches without formatting complete inventories or file contents."""
    if isinstance(value, bytes):
        return f"bytes(length={len(value)}, sha256={hashlib.sha256(value).hexdigest()})"
    if isinstance(value, dict) and isinstance(value.get("sha256"), str):
        return f"dict(length={len(value)}, sha256={_diagnostic_value(value['sha256'])})"
    if isinstance(value, (dict, list, tuple, set, frozenset)):
        return f"{type(value).__name__}(length={len(value)})"
    if isinstance(value, str):
        return repr(value[:120]) + ("..." if len(value) > 120 else "")
    if value is None or isinstance(value, (bool, int, float)):
        return repr(value)
    return f"<{type(value).__name__}>"


class IdentityMismatch(ValueError):
    """Keep full evidence for an explicitly requested local diagnostic artifact."""

    def __init__(self, label: str, actual: object, expected: object):
        self.label, self.actual, self.expected = label, actual, expected
        detail = f"expected {_diagnostic_value(expected)}, found {_diagnostic_value(actual)}"
        if isinstance(actual, dict) and isinstance(expected, dict):
            missing = expected.keys() - actual.keys()
            extra = actual.keys() - expected.keys()
            changed = [key for key in expected.keys() & actual.keys()
                       if actual[key] != expected[key]]
            parts = []
            for kind, keys in (("missing", missing), ("extra", extra), ("changed", changed)):
                sample = sorted(keys, key=str)[:5]
                values = ", ".join(_diagnostic_value(key) for key in sample)
                parts.append(f"{kind}={len(keys)} [{values}]")
            detail += "; " + "; ".join(parts)
            for key in sorted(changed, key=str)[:3]:
                detail += (f"; {_diagnostic_value(key)}: expected "
                           f"{_diagnostic_value(expected[key])}, found "
                           f"{_diagnostic_value(actual[key])}")
        elif isinstance(actual, (set, frozenset)) and isinstance(expected, (set, frozenset)):
            for kind, keys in (("missing", expected - actual), ("extra", actual - expected)):
                sample = ", ".join(_diagnostic_value(key) for key in sorted(keys, key=str)[:5])
                detail += f"; {kind}={len(keys)} [{sample}]"
        super().__init__(f"{label[:160]} mismatch: {detail}")


def _require_equal(label: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise IdentityMismatch(label, actual, expected)


def _diagnostic_json_default(value: object) -> object:
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    if isinstance(value, Path):
        return str(value)
    return {"type": type(value).__name__}


def artifact_stem(application_version: str, mode: str) -> str:
    if mode not in TRUST_CLASSIFICATIONS:
        raise ValueError(f"unsupported release mode: {mode}")
    if not re.fullmatch(r"\d+\.\d+\.\d+", application_version):
        raise ValueError(f"invalid application version: {application_version!r}")
    return f"MBUprime-StructLab-{application_version}-windows-x64-{mode}"


def verify_source_identity(
    project_root: Path = PROJECT_ROOT,
    provenance_path: Path = PROVENANCE_PATH,
) -> dict[str, object]:
    """Verify checked provenance against the current source policy and live files."""
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    import scientific_metadata as sm

    _require_equal("live manifest schema", sm.MANIFEST_SCHEMA_VERSION,
                   REQUIRED_MANIFEST_SCHEMA_VERSION)
    _require_equal("live scientific policy", sm.SCIENTIFIC_POLICY_VERSION,
                   REQUIRED_SCIENTIFIC_POLICY_VERSION)
    corresponding = " ".join(
        (project_root / "CORRESPONDING_SOURCE.md").read_text(
            encoding="utf-8").split())
    expected_source = (
        f"The current source target is app {sm.APPLICATION_VERSION}, manifest "
        f"schema {sm.MANIFEST_SCHEMA_VERSION}, policy "
        f"`{sm.SCIENTIFIC_POLICY_VERSION}`.")
    expected_release = (
        f"The locally built distribution targets app {sm.APPLICATION_VERSION}, manifest "
        f"schema {sm.MANIFEST_SCHEMA_VERSION}, and this policy.")
    if expected_source not in corresponding or expected_release not in corresponding:
        raise ValueError(
            "CORRESPONDING_SOURCE.md scientific identity is stale")
    provenance = _load_json(provenance_path)
    expected_identity = {
        "application_version": sm.APPLICATION_VERSION,
        "schema_version": REQUIRED_MANIFEST_SCHEMA_VERSION,
        "scientific_policy_version": REQUIRED_SCIENTIFIC_POLICY_VERSION,
        "source_tree_sha256": sm.compute_source_tree_hash(project_root),
        "requirements_lock_sha256": _sha256(project_root / "requirements-lock.txt"),
    }
    for key, expected in expected_identity.items():
        _require_equal(f"provenance {key}", provenance.get(key), expected)
    dependencies = provenance.get("dependencies")
    if not isinstance(dependencies, dict):
        raise ValueError("provenance dependencies are missing")
    primer3_identity = sm.validated_primer3_runtime_identity()
    vienna_identity = sm.validated_vienna_runtime_identity()
    _require_equal(
        "provenance Primer3 runtime identity",
        dependencies.get("primer3_runtime_identity"), primer3_identity)
    _require_equal(
        "provenance ViennaRNA runtime identity",
        dependencies.get("viennarna_runtime_identity"), vienna_identity)
    from native_target import current_target

    native_manifest_path = (
        project_root / "rnastructure_native" / "native_manifests" /
        f"{current_target()}.json")
    native_manifest = _load_json(native_manifest_path)
    native_keys = (
        "integration", "engine_version", "target", "abi_tag", "compiler",
        "native_module_sha256", "upstream_source_archive_sha256",
        "upstream_source_manifest_sha256", "source_manifest_sha256",
        "dna_table_manifest_sha256", "compatibility_patch",
    )
    _require_equal(
        "provenance RNAstructure native identity",
        provenance.get("rnastructure_native"),
        {key: native_manifest[key] for key in native_keys},
    )
    _require_equal(
        "provenance RNAstructure native manifest hash",
        provenance.get("rnastructure_native_manifest_sha256"),
        _sha256(native_manifest_path),
    )
    return provenance


def _verify_sidecar(path: Path, expected_hash: str, expected_name: str) -> None:
    expected = f"{expected_hash} *{expected_name}"
    _require_equal(f"sidecar {path.name}", path.read_text(encoding="ascii"), expected)


def _verify_pe_amd64(executable: Path) -> None:
    with executable.open("rb") as handle:
        if handle.read(2) != b"MZ":
            raise ValueError("main executable lacks a DOS MZ header")
        handle.seek(0x3C)
        offset_bytes = handle.read(4)
        if len(offset_bytes) != 4:
            raise ValueError("main executable has a truncated DOS header")
        pe_offset = struct.unpack("<I", offset_bytes)[0]
        handle.seek(pe_offset)
        if handle.read(4) != b"PE\0\0":
            raise ValueError("main executable lacks a PE signature")
        machine_bytes = handle.read(2)
    if len(machine_bytes) != 2 or struct.unpack("<H", machine_bytes)[0] != 0x8664:
        raise ValueError("main executable PE machine is not AMD64")


def _authenticode_identity(executable: Path) -> dict[str, object]:
    script = r"""
$ErrorActionPreference = "Stop"
$signature = Get-AuthenticodeSignature -LiteralPath $env:MBUPRIME_VERIFY_EXE
$certificate = $signature.SignerCertificate
$chainTrusted = $false
if ($null -ne $certificate) { $chainTrusted = $certificate.Verify() }
$status = [string]$signature.Status
$statusReason = switch ($status) {
    "NotSigned" { "no_authenticode_signature" }
    "Valid" { "valid_authenticode_signature" }
    "HashMismatch" { "authenticode_hash_mismatch" }
    "NotTrusted" { "authenticode_not_trusted" }
    "NotSupportedFileFormat" { "authenticode_format_not_supported" }
    "Incompatible" { "authenticode_incompatible" }
    default { "authenticode_status_" + ($status -replace "[^A-Za-z0-9]+", "_").ToLowerInvariant() }
}
[ordered]@{
    status = $status
    status_reason = $statusReason
    chain_trusted_on_verification_host = [bool]$chainTrusted
    signer_thumbprint = if ($null -ne $certificate) { [string]$certificate.Thumbprint } else { "" }
    signer_subject = if ($null -ne $certificate) { [string]$certificate.Subject } else { "" }
} | ConvertTo-Json -Compress
"""
    environment = os.environ.copy()
    environment["MBUPRIME_VERIFY_EXE"] = str(executable)
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        env=environment,
    )
    if completed.returncode:
        raise ValueError(f"Authenticode verification failed: {completed.stderr.strip()}")
    payload = json.loads(completed.stdout)
    if not isinstance(payload, dict):
        raise ValueError("Authenticode verification returned an invalid identity")
    return payload


def _verify_trust(executable: Path, attestation: dict[str, object], mode: str) -> None:
    expected_classification = TRUST_CLASSIFICATIONS[mode]
    _require_equal("signing mode", attestation.get("mode"), mode)
    _require_equal("trust classification", attestation.get("trust_classification"),
                   expected_classification)
    actual = _authenticode_identity(executable)
    recorded = attestation.get("authenticode")
    if not isinstance(recorded, dict):
        raise ValueError("signing attestation lacks Authenticode identity")
    expected_identity_fields = {
        "status", "status_reason", "chain_trusted_on_verification_host",
        "signer_thumbprint", "signer_subject",
    }
    _require_equal("signing attestation identity fields", set(recorded),
                   expected_identity_fields)
    for field in ("status", "status_reason", "chain_trusted_on_verification_host"):
        _require_equal(f"verified {field}", actual.get(field), recorded.get(field))
    if mode == "unsigned":
        _require_equal("Authenticode status", actual.get("status"), "NotSigned")
        _require_equal("unsigned signer thumbprint", actual.get("signer_thumbprint"), "")
    else:
        _require_equal("Authenticode status", actual.get("status"), "Valid")
        _require_equal("trusted chain on verification host",
                       actual.get("chain_trusted_on_verification_host"), True)
        for field in ("signer_thumbprint", "signer_subject"):
            _require_equal(f"verified {field}", actual.get(field), recorded.get(field))


def _application_file_records(application_dir: Path) -> dict[str, dict[str, object]]:
    return {
        path.relative_to(application_dir).as_posix(): {
            "path": path.relative_to(application_dir).as_posix(),
            "size": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in sorted(application_dir.rglob("*"))
        if path.is_file()
    }


def _verify_release_root_topology(application_dir: Path) -> None:
    entries = {path.name: path for path in application_dir.iterdir()}
    _require_equal("release root entries", frozenset(entries), RELEASE_ROOT_ENTRIES)
    if not entries[EXECUTABLE_NAME].is_file():
        raise ValueError("release root executable is not a file")
    if not entries["_internal"].is_dir():
        raise ValueError("release root _internal payload is not a directory")


def _normalized_archive_names(archive: zipfile.ZipFile) -> set[str]:
    names: set[str] = set()
    for raw_name in archive.namelist():
        name = raw_name.replace("\\", "/")
        parts = [part for part in name.rstrip("/").split("/") if part]
        if (raw_name != name or name.startswith("/") or "//" in name
                or re.match(r"^[A-Za-z]:", name)
                or any(part in (".", "..") for part in parts)):
            raise ValueError(f"archive contains an unsafe member: {raw_name!r}")
        if not parts or name.endswith("/"):
            continue
        normalized = "/".join(parts)
        if normalized in names:
            raise ValueError(f"archive contains a duplicate member: {normalized}")
        names.add(normalized)
    return names


def _verify_archive_topology(
    archive: zipfile.ZipFile,
    expected_files: set[str],
) -> set[str]:
    names = _normalized_archive_names(archive)
    _require_equal("archived release tree", names, expected_files)
    root_entries = frozenset(
        name.replace("\\", "/").lstrip("/").split("/", 1)[0]
        for name in archive.namelist() if name.strip("/\\")
    )
    _require_equal("archive root entries", root_entries, RELEASE_ROOT_ENTRIES)
    return names


def _manifest_file_records(payload: object) -> dict[str, dict[str, object]]:
    if not isinstance(payload, list):
        raise ValueError("release manifest application files must be a list")
    records: dict[str, dict[str, object]] = {}
    for record in payload:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise ValueError("release manifest contains an invalid application file record")
        path = str(record["path"])
        if path in records:
            raise ValueError(f"duplicate application file identity: {path}")
        records[path] = record
    return records


def _source_inventory_target(
    project_root: Path,
    target_name: str,
) -> tuple[dict[str, object], set[str], set[str]]:
    inventory = _load_json(project_root / INVENTORY_RELATIVE_PATH)
    target = inventory["targets"][target_name]
    files: set[str] = set()
    trees: set[str] = set()
    for group_name in target["groups"]:
        group = inventory["source_groups"][group_name]
        files.update(group.get("files", ()))
        trees.update(group.get("trees", ()))
    for artifact_name in target.get("generated_artifacts", ()):
        files.add(inventory["generated_artifacts"][artifact_name]["path"])
    return inventory, files, trees


def _verify_source_staging(
    source_root: Path,
    project_root: Path,
) -> dict[str, str]:
    manifest_path = source_root / "source-manifest.json"
    manifest = _load_json(manifest_path)
    actual = {
        path.relative_to(source_root).as_posix(): _sha256(path)
        for path in sorted(source_root.rglob("*"))
        if path.is_file() and path != manifest_path
    }
    _require_equal("corresponding-source manifest", manifest, actual)
    _, declared_files, declared_trees = _source_inventory_target(
        project_root, "windows_corresponding_source")
    expected_files = set(declared_files)
    for tree in declared_trees:
        root_tree = project_root / tree
        if not root_tree.is_dir():
            raise ValueError(f"declared corresponding-source tree is absent: {tree}")
        expected_files.update(
            path.relative_to(project_root).as_posix()
            for path in root_tree.rglob("*") if path.is_file())
    _require_equal("canonical corresponding-source set", set(actual), expected_files)
    for relative in sorted(expected_files):
        root_path = project_root / relative
        if not root_path.is_file():
            raise ValueError(f"declared corresponding source is absent: {relative}")
        _require_equal(
            f"corresponding-source root hash {relative}",
            actual[relative], _sha256(root_path))
    return actual


def _executable_project_modules(
    executable: Path,
    project_root: Path,
) -> dict[str, str]:
    from PyInstaller.archive.readers import CArchiveReader

    pyz = CArchiveReader(str(executable)).open_embedded_archive("PYZ.pyz")
    owned: dict[str, str] = {}
    for module_name in pyz.toc:
        module_path = project_root.joinpath(*module_name.split("."))
        candidates = (module_path.with_suffix(".py"), module_path / "__init__.py")
        source = next((path for path in candidates if path.is_file()), None)
        if source is not None:
            owned[module_name] = source.relative_to(project_root).as_posix()
    return owned


def _verify_maintained_module_source_closure(
    executable: Path,
    source_files: dict[str, str],
    project_root: Path,
) -> dict[str, str]:
    inventory = _load_json(project_root / INVENTORY_RELATIVE_PATH)
    maintained = {
        str(module): str(source)
        for module, source in inventory["maintained_modules"].items()
    }
    discovered = _executable_project_modules(executable, project_root)
    _require_equal("EXE maintained module set", discovered, maintained)
    for module, relative in maintained.items():
        if relative not in source_files:
            raise ValueError(
                f"corresponding source lacks EXE maintained module {module}: {relative}")
        _require_equal(
            f"EXE maintained source hash {module}",
            source_files[relative], _sha256(project_root / relative))
    from PyInstaller.archive.readers import CArchiveReader

    archive = CArchiveReader(str(executable))
    pyz = archive.open_embedded_archive("PYZ.pyz")
    for module, relative in maintained.items():
        _verify_frozen_code(module, pyz.extract(module), project_root / relative)
    bootstraps = inventory["maintained_bootstraps"]
    # Discover owned scripts independently of the declared mapping, including
    # unexpected first-party runtime hooks. Third-party bootloader hooks are not
    # compared to this project's source.
    declared_files = {
        relative for group in inventory["source_groups"].values()
        for relative in group.get("files", ()) if relative.endswith(".py")
    }
    discovered_bootstraps = {
        Path(relative).stem: relative for relative in declared_files
        if Path(relative).stem in archive.toc
        and archive.toc[Path(relative).stem][-1] == "s"
    }
    _require_equal("EXE maintained bootstrap set", discovered_bootstraps, bootstraps)
    for name, relative in bootstraps.items():
        if relative not in source_files:
            raise ValueError(f"corresponding source lacks EXE bootstrap {name}: {relative}")
        _require_equal(f"EXE bootstrap source hash {name}",
                       source_files[relative], _sha256(project_root / relative))
        _verify_frozen_code(name, marshal.loads(archive.extract(name)),
                            project_root / relative)
    return discovered


def _normalized_frozen_code(code: CodeType) -> tuple:
    """Ignore build paths/debug positions; retain executable code and constants.

    Compare every CPython 3.12 execution field explicitly (CodeType equality
    omits stack size and qualified name). Exclude only filename and source
    positions, which PyInstaller rewrites and comments/blank lines can shift.
    Docstrings remain constants because the release uses optimize=1, not 2.
    """
    if not isinstance(code, CodeType):
        raise ValueError("EXE maintained entry is not a Python code object")
    return (
        code.co_argcount, code.co_posonlyargcount, code.co_kwonlyargcount,
        code.co_nlocals, code.co_stacksize, code.co_flags, code.co_code,
        code.co_names, code.co_varnames, code.co_name, code.co_qualname,
        code.co_freevars, code.co_cellvars, code.co_exceptiontable,
        tuple(_normalized_code_constant(value) for value in code.co_consts),
    )


def _normalized_code_constant(value):
    # Python equality conflates True/1 and -0.0/0.0. These are observable
    # constants, so retain their types and floating-point representations.
    if isinstance(value, CodeType):
        return CodeType, _normalized_frozen_code(value)
    if isinstance(value, tuple):
        return tuple, tuple(_normalized_code_constant(item) for item in value)
    if isinstance(value, frozenset):
        return frozenset, frozenset(_normalized_code_constant(item) for item in value)
    if isinstance(value, (float, complex)):
        return type(value), marshal.dumps(value)
    return type(value), value


def _verify_frozen_code(name: str, frozen: CodeType, source: Path) -> None:
    expected = compile(source.read_bytes(), str(source), "exec",
                       dont_inherit=True, optimize=1)
    if _normalized_frozen_code(frozen) != _normalized_frozen_code(expected):
        raise ValueError(f"EXE maintained code mismatch: {name} ({source.name})")


def _packaged_layer_inventory(
    executable: Path,
    application_dir: Path,
) -> dict[str, set[str]]:
    from PyInstaller.archive.readers import CArchiveReader

    archive = CArchiveReader(str(executable))
    carchive = set(archive.toc)
    pyz = set(archive.open_embedded_archive("PYZ.pyz").toc)
    return {
        "physical": {
            path.relative_to(application_dir).as_posix()
            for path in application_dir.rglob("*") if path.is_file()
        },
        "pyz": pyz,
        "carchive": carchive,
        "hooks": {name for name in carchive if name.startswith("pyi_rth_")},
    }


def _payload_rule_matches(entry: str, layer: str, rule: dict[str, object]) -> bool:
    if layer not in rule.get("enforced_layers", ()):
        return False
    normalized = entry.replace("\\", "/").lower().lstrip("./")
    payload_path = normalized.removeprefix("_internal/")
    module_name = payload_path.replace("/", ".")
    for prefix in rule.get("module_prefixes", ()):
        lowered = str(prefix).lower()
        if module_name == lowered or module_name.startswith(f"{lowered}."):
            return True
    if any(payload_path.startswith(str(prefix).lower())
           for prefix in rule.get("path_prefixes", ())):
        return True
    if any(fnmatch.fnmatch(payload_path, str(pattern).lower())
           for pattern in rule.get("path_globs", ())):
        return True
    if layer == "hooks":
        hook = payload_path.rsplit("/", 1)[-1].removesuffix(".py")
        if any(hook == str(name).lower().removesuffix(".py")
               for name in rule.get("hook_names", ())):
            return True
    return False


def _forbidden_payload_findings(
    layers: dict[str, set[str]],
    inventory_path: Path,
) -> dict[str, dict[str, list[str]]]:
    inventory = _load_json(inventory_path)
    policy = inventory["clean_build_exclusions"]
    return {
        family: {
            layer: sorted(
                entry for entry in layers.get(layer, set())
                if _payload_rule_matches(entry, layer, rule))
            for layer in policy["layers"]
        }
        for family, rule in policy["families"].items()
    }


def _require_no_forbidden_payload(
    findings: dict[str, dict[str, list[str]]],
) -> None:
    offenders = {
        family: {layer: entries for layer, entries in layer_hits.items() if entries}
        for family, layer_hits in findings.items()
        if any(layer_hits.values())
    }
    if offenders:
        raise ValueError(f"canonical excluded payload remains: {offenders}")


def _allowlisted_payload_identity(
    application_dir: Path,
    inventory_path: Path,
) -> dict[str, object]:
    inventory = _load_json(inventory_path)
    policy = inventory["payload_allowlists"]["matplotlib_fonts"]
    prefix = str(policy["path_prefix"])
    root = application_dir / "_internal" / Path(prefix)
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*") if path.is_file()
    } if root.is_dir() else set()
    expected = set(policy["files"])
    if actual != expected:
        raise ValueError(
            "matplotlib font allowlist mismatch: "
            f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}")
    return {"matplotlib_fonts": {
        "path_prefix": prefix,
        "file_count": len(actual),
        "files": sorted(actual),
    }}


def _require_no_excluded_evidence(
    release_evidence: dict[str, object],
    inventory: dict[str, object],
) -> None:
    forbidden = {
        component_id
        for rule in inventory["clean_build_exclusions"]["families"].values()
        for component_id in rule.get("evidence_component_ids", ())
    }
    present = {
        component["id"] for component in release_evidence["components"]
    }.intersection(forbidden)
    if present:
        raise ValueError(f"excluded evidence components remain: {sorted(present)}")


def _protected_payload_identity(application_dir: Path) -> dict[str, object]:
    internal = application_dir / "_internal"
    provenance_path = internal / "release_provenance.json"
    if not provenance_path.is_file():
        raise ValueError("protected payload is absent: release_provenance.json")
    provenance = _load_json(provenance_path)
    dependencies = provenance.get("dependencies")
    if not isinstance(dependencies, dict):
        raise ValueError("bundled provenance dependencies are missing")
    required = {
        "corresponding_source_archive": (
            "rnastructure-corresponding-source/vendor/rnastructure-6.6/"
            "RNAstructureSource.zip"),
        "cascadia_font": "assets/fonts/CascadiaMono.ttf",
        "cascadia_license": "assets/fonts/OFL.txt",
        "cascadia_source": "assets/fonts/SOURCE.txt",
        "tk_script": "_tk_data/tk.tcl",
        "tk_extension": "_tkinter.pyd",
        "tcl_dll": "tcl86t.dll",
        "tk_dll": "tk86t.dll",
        "primer3_metadata": "primer3_py-2.3.0.dist-info/METADATA",
        "seqfold_metadata": "seqfold-0.10.2.dist-info/METADATA",
    }
    identities: dict[str, object] = {}
    for label, relative in required.items():
        path = internal / relative
        if not path.is_file():
            raise ValueError(f"protected payload is absent: {relative}")
        identities[label] = {"path": relative, "sha256": _sha256(path)}
    for label, identity_key, package_dir, filename_key, hash_key in (
        ("primer3_thermoanalysis", "primer3_runtime_identity", "primer3",
         "thermoanalysis_filename", "thermoanalysis_sha256"),
        ("primer3_p3helpers", "primer3_runtime_identity", "primer3",
         "p3helpers_filename", "p3helpers_sha256"),
        ("vienna_native", "viennarna_runtime_identity", "RNA",
         "native_binding_filename", "native_binding_sha256"),
    ):
        identity = dependencies.get(identity_key)
        if not isinstance(identity, dict):
            raise ValueError(f"bundled provenance lacks {identity_key}")
        filename = identity.get(filename_key)
        expected_hash = identity.get(hash_key)
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise ValueError(f"bundled provenance has invalid {filename_key}")
        if not isinstance(expected_hash, str) or not re.fullmatch(
                r"[0-9a-f]{64}", expected_hash):
            raise ValueError(f"bundled provenance has invalid {hash_key}")
        relative = f"{package_dir}/{filename}"
        path = internal / relative
        if not path.is_file():
            raise ValueError(f"protected payload is absent: {relative}")
        actual_hash = _sha256(path)
        _require_equal(f"protected payload {label}", actual_hash, expected_hash)
        identities[label] = {"path": relative, "sha256": actual_hash}
    for label, root in (
        ("primer3_config", internal / "primer3" / "src" / "libprimer3" /
         "primer3_config"),
        ("matplotlib_fonts", internal / "matplotlib" / "mpl-data" / "fonts"),
    ):
        files = sorted(path for path in root.rglob("*") if path.is_file())
        if not files:
            raise ValueError(f"protected payload family is absent: {label}")
        identities[label] = {"file_count": len(files)}
    return identities


def _parse_self_test_log(log_path: Path) -> tuple[dict[str, str], str]:
    text = log_path.read_text(encoding="utf-8")
    identity_line = next(
        (line for line in text.splitlines() if line.startswith("Self-test OK: ")),
        None,
    )
    if identity_line is None:
        raise ValueError(f"packaged self-test log lacks success identity: {log_path}")
    identity = dict(
        item.split("=", 1) for item in identity_line.removeprefix("Self-test OK: ").split(", ")
    )
    completed_stages = set(text.splitlines())
    missing = REQUIRED_SELF_TEST_STAGES - completed_stages
    if missing:
        raise ValueError(f"packaged self-test omitted stages: {sorted(missing)}")
    if "stage=tk-root-ready" not in text:
        raise ValueError("packaged self-test did not create a real Tk root")
    if "stage=font-register status=ok family=Cascadia Mono" not in text:
        raise ValueError("packaged self-test did not register the bundled font")
    return identity, text


def _run_self_test(
    executable: Path,
    log_dir: Path,
    label: str,
    temp_path: Path | None,
) -> dict[str, str]:
    log_path = log_dir / f"self-test-{label}.log"
    environment = os.environ.copy()
    if temp_path is None:
        environment.pop("TEMP", None)
        environment.pop("TMP", None)
    else:
        environment["TEMP"] = str(temp_path)
        environment["TMP"] = str(temp_path)
    completed = subprocess.run(
        [str(executable), "--self-test", "--self-test-log", str(log_path)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=180,
        env=environment,
    )
    if completed.returncode:
        details = log_path.read_text(encoding="utf-8") if log_path.exists() else "no log"
        raise ValueError(
            f"packaged self-test {label} exited {completed.returncode}: {details}")
    identity, _ = _parse_self_test_log(log_path)
    return identity


@contextmanager
def _non_writable_path(parent: Path) -> Iterator[Path | None]:
    restricted = parent / "existing-non-writable-temp"
    restricted.write_bytes(b"read-only TEMP/TMP path")
    restricted.chmod(0o444)
    try:
        try:
            restricted.write_bytes(b"permission probe")
        except OSError:
            yield restricted
        else:
            yield None
    finally:
        restricted.chmod(0o666)


def _expected_self_test_identity(
    provenance: dict[str, object], executable_hash: str,
) -> dict[str, object]:
    expected = {
        "application": provenance["application_version"],
        "scientific_policy": provenance["scientific_policy_version"],
        "source_tree_sha256": provenance["source_tree_sha256"],
        "dependency_lock_sha256": provenance["requirements_lock_sha256"],
        "artifact_sha256": executable_hash,
        "font": "Cascadia Mono",
        "primer3": "ok",
        "matplotlib": "ok",
        "diagram_exports": "png,pdf,svg",
        "archive_schema": "2",
        "legacy_archive_schema": "1",
        "streamed_record_limit": "5000000",
    }
    dependencies = provenance.get("dependencies")
    if not isinstance(dependencies, dict):
        raise ValueError("provenance dependencies are missing")
    primer3_identity = dependencies.get("primer3_runtime_identity")
    vienna_identity = dependencies.get("viennarna_runtime_identity")
    if not isinstance(primer3_identity, dict):
        raise ValueError("provenance Primer3 runtime identity is missing")
    if not isinstance(vienna_identity, dict):
        raise ValueError("provenance ViennaRNA runtime identity is missing")
    expected.update({
        "primer3_core": primer3_identity["libprimer3_version"],
        "primer3_target": primer3_identity["target"],
        "primer3_thermoanalysis": primer3_identity["thermoanalysis_filename"],
        "primer3_thermoanalysis_sha256": primer3_identity["thermoanalysis_sha256"],
        "primer3_p3helpers": primer3_identity["p3helpers_filename"],
        "primer3_p3helpers_sha256": primer3_identity["p3helpers_sha256"],
        "vienna": vienna_identity["version"],
        "vienna_target": vienna_identity["target"],
        "vienna_native": vienna_identity["native_binding_filename"],
        "vienna_native_sha256": vienna_identity["native_binding_sha256"],
    })
    return expected


def _verify_self_test_probes(
    identities: list[dict[str, str]], expected: dict[str, object],
) -> None:
    for index, identity in enumerate(identities):
        for key, value in expected.items():
            _require_equal(
                f"packaged self-test {index} {key}", identity.get(key), value)
        for engine in ("vienna", "seqfold", "rnastructure_native"):
            if not identity.get(engine):
                raise ValueError(f"packaged self-test lacks {engine} identity")


def _verify_cyrillic_path_probe(
    application_dir: Path, verifier_root: Path,
) -> int:
    cyrillic_parent = verifier_root / "\u041a\u0438\u0440\u0438\u043b\u043b\u0438\u0446\u0430"
    copied_application = cyrillic_parent / APPLICATION_DIR_NAME
    shutil.copytree(application_dir, copied_application)
    copied_executable = copied_application / EXECUTABLE_NAME
    path_log = verifier_root / "cyrillic-path.log"
    completed = subprocess.run(
        [str(copied_executable), "--self-test", "--self-test-log", str(path_log)],
        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=60, env=os.environ.copy())
    _require_equal("Cyrillic-path controlled exit", completed.returncode, 3)
    path_text = path_log.read_text(encoding="utf-8")
    tk_position = path_text.find("stage=tk-root-ready")
    failure_position = path_text.find("stage=install-path-check status=failed")
    if tk_position < 0 or failure_position <= tk_position:
        raise ValueError(
            "Cyrillic-path probe did not record Tk readiness before controlled path rejection")
    return completed.returncode


@contextmanager
def _self_test_directory(parent: Path) -> Iterator[Path]:
    temporary = tempfile.TemporaryDirectory(prefix="release-verifier-", dir=parent)
    try:
        yield Path(temporary.name)
    finally:
        # Windows may briefly retain the copied executable after its process exits.
        for attempt in range(10):
            try:
                temporary.cleanup()
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.5)


def _verify_self_test_identity(
    executable: Path,
    application_dir: Path,
    provenance: dict[str, object],
    executable_hash: str,
) -> dict[str, object]:
    with _self_test_directory(application_dir.parent) as verifier_root:
        identities = [
            _run_self_test(executable, verifier_root, "temp-unset", None),
            _run_self_test(
                executable, verifier_root, "temp-nonexistent",
                verifier_root / "does-not-exist"),
        ]
        with _non_writable_path(verifier_root) as restricted:
            if restricted is not None:
                identities.append(_run_self_test(
                    executable, verifier_root, "temp-non-writable", restricted))
            else:
                print("Release verifier: non-writable TEMP probe skipped; host ACL semantics did not enforce it.")

        expected = _expected_self_test_identity(provenance, executable_hash)
        _verify_self_test_probes(identities, expected)
        cyrillic_exit = _verify_cyrillic_path_probe(
            application_dir, verifier_root)
        return {
            "result": "passed",
            "required_stages": sorted(REQUIRED_SELF_TEST_STAGES),
            "probe_count": len(identities),
            "identity": identities[0],
            "cyrillic_path_controlled_exit": cyrillic_exit,
        }


_PackageVerificationPaths = namedtuple(
    "_PackageVerificationPaths",
    (
        "project_root", "provenance_path", "application_dir", "archive",
        "release_manifest_path", "signing_attestation_path", "executable",
        "stem", "release_mode",
    ),
)
_PackageVerificationHashes = namedtuple(
    "_PackageVerificationHashes",
    ("executable", "archive", "provenance", "attestation"),
)


def _package_verification_paths(
    project_root: Path,
    provenance_path: Path,
    application_dir: Path | None,
    archive: Path | None,
    release_manifest_path: Path | None,
    signing_attestation_path: Path | None,
    release_mode: str,
    application_version: str,
) -> _PackageVerificationPaths:
    """Freeze every path derived for one package-verification run."""

    stem = artifact_stem(application_version, release_mode)
    dist = project_root / "dist"
    resolved_application = application_dir or dist / APPLICATION_DIR_NAME
    resolved_archive = archive or dist / f"{stem}.zip"
    resolved_release_manifest = (
        release_manifest_path or dist / f"{stem}.release-manifest.json")
    resolved_attestation = (
        signing_attestation_path or dist / SIGNING_ATTESTATION_NAME)
    return _PackageVerificationPaths(
        project_root=project_root,
        provenance_path=provenance_path,
        application_dir=resolved_application,
        archive=resolved_archive,
        release_manifest_path=resolved_release_manifest,
        signing_attestation_path=resolved_attestation,
        executable=resolved_application / EXECUTABLE_NAME,
        stem=stem,
        release_mode=release_mode,
    )


def _verify_package_manifest_identity(
    paths: _PackageVerificationPaths,
    provenance: dict[str, object],
    release: dict[str, object],
    attestation: dict[str, object],
) -> _PackageVerificationHashes:
    """Verify trust, binary identity, and top-level release records."""

    _verify_trust(paths.executable, attestation, paths.release_mode)
    _verify_pe_amd64(paths.executable)
    hashes = _PackageVerificationHashes(
        executable=_sha256(paths.executable),
        archive=_sha256(paths.archive),
        provenance=_sha256(paths.provenance_path),
        attestation=_sha256(paths.signing_attestation_path),
    )
    _require_equal("release manifest schema", release.get("release_manifest_schema"), "2")
    _require_equal("release mode", release.get("release_mode"), paths.release_mode)
    _require_equal("release trust classification", release.get("trust_classification"),
                   TRUST_CLASSIFICATIONS[paths.release_mode])
    _require_equal("release scientific identity", release.get("application_identity"), {
        "application_version": provenance["application_version"],
        "schema_version": REQUIRED_MANIFEST_SCHEMA_VERSION,
        "scientific_policy_version": REQUIRED_SCIENTIFIC_POLICY_VERSION,
    })
    for label, record, expected_name, expected_hash in (
        ("executable", release.get("executable"), EXECUTABLE_NAME, hashes.executable),
        ("archive", release.get("archive"), paths.archive.name, hashes.archive),
        ("provenance", release.get("provenance"), paths.provenance_path.name,
         hashes.provenance),
        ("signing attestation", release.get("signing_attestation"),
         paths.signing_attestation_path.name, hashes.attestation),
    ):
        if not isinstance(record, dict):
            raise ValueError(f"release manifest lacks {label} identity")
        _require_equal(f"{label} name", record.get("name"), expected_name)
        _require_equal(f"{label} SHA-256", record.get("sha256"), expected_hash)
    _require_equal("executable PE machine", release["executable"].get("pe_machine"), "AMD64")
    return hashes


def _verify_package_application_folder(
    paths: _PackageVerificationPaths,
    release: dict[str, object],
    hashes: _PackageVerificationHashes,
) -> tuple[dict[str, dict[str, object]], Path]:
    """Verify sidecars, application files, and bundled support identities."""

    executable_sidecar = paths.application_dir.parent / EXECUTABLE_SIDECAR_NAME
    _verify_sidecar(executable_sidecar, hashes.executable, paths.executable.name)
    sidecar_record = release.get("executable_sidecar")
    if not isinstance(sidecar_record, dict):
        raise ValueError("release manifest lacks executable sidecar identity")
    _require_equal("executable sidecar name", sidecar_record.get("name"),
                   EXECUTABLE_SIDECAR_NAME)
    _require_equal("executable sidecar internal path",
                   sidecar_record.get("internal_path"),
                   f"_internal/{EXECUTABLE_SIDECAR_NAME}")
    _require_equal("executable sidecar SHA-256", sidecar_record.get("sha256"),
                   _sha256(executable_sidecar))
    _require_equal("provenance internal path", release["provenance"].get("internal_path"),
                   "_internal/release_provenance.json")
    _require_equal("signing attestation internal path",
                   release["signing_attestation"].get("internal_path"),
                   f"_internal/{SIGNING_ATTESTATION_NAME}")
    _verify_sidecar(Path(f"{paths.archive}.sha256"), hashes.archive,
                    paths.archive.name)
    actual_files = _application_file_records(paths.application_dir)
    application_record = release.get("application_folder")
    if not isinstance(application_record, dict):
        raise ValueError("release manifest lacks application folder identity")
    _require_equal("application folder name", application_record.get("name"),
                   APPLICATION_DIR_NAME)
    recorded_files = _manifest_file_records(application_record.get("files"))
    _require_equal("recursive application file identities", recorded_files,
                   actual_files)
    for required in REQUIRED_APP_FILES:
        if required not in actual_files:
            raise ValueError(f"packaged application lacks {required}")

    internal_dir = paths.application_dir / "_internal"
    bundled_provenance = internal_dir / "release_provenance.json"
    _require_equal("bundled provenance", bundled_provenance.read_bytes(),
                   paths.provenance_path.read_bytes())
    _require_equal(
        "bundled executable sidecar",
        (internal_dir / EXECUTABLE_SIDECAR_NAME).read_bytes(),
        executable_sidecar.read_bytes(),
    )
    _require_equal(
        "bundled signing attestation",
        (internal_dir / SIGNING_ATTESTATION_NAME).read_bytes(),
        paths.signing_attestation_path.read_bytes(),
    )
    for name in ("README.md", "COPYING", "THIRD_PARTY_NOTICES.md",
                 "CORRESPONDING_SOURCE.md"):
        _require_equal(
            f"bundled support file {name}",
            (internal_dir / name).read_bytes(),
            (paths.project_root / name).read_bytes(),
        )
    return actual_files, internal_dir


def _verify_package_corresponding_source(
    paths: _PackageVerificationPaths,
    release: dict[str, object],
    internal_dir: Path,
) -> tuple[dict[str, str], tuple[str, ...], Path]:
    """Verify staged sources and executable-to-source module closure."""

    source_root = internal_dir / "rnastructure-corresponding-source"
    source_files = _verify_source_staging(source_root, paths.project_root)
    maintained_modules = _verify_maintained_module_source_closure(
        paths.executable, source_files, paths.project_root)
    source_manifest_path = source_root / "source-manifest.json"
    source_record = release.get("corresponding_source")
    if not isinstance(source_record, dict):
        raise ValueError("release manifest lacks corresponding-source identity")
    _require_equal("corresponding-source directory", source_record.get("directory"),
                   "_internal/rnastructure-corresponding-source")
    _require_equal("corresponding-source manifest name", source_record.get("manifest"),
                   source_manifest_path.name)
    _require_equal("corresponding-source manifest SHA-256",
                   source_record.get("manifest_sha256"),
                   _sha256(source_manifest_path))
    return source_files, maintained_modules, source_manifest_path


def _verify_package_archive(
    paths: _PackageVerificationPaths,
    actual_files: dict[str, dict[str, object]],
    source_files: dict[str, str],
    source_manifest_path: Path,
) -> None:
    """Verify archive topology and byte parity with application/source files."""

    with zipfile.ZipFile(paths.archive) as bundled:
        archive_names = _verify_archive_topology(bundled, set(actual_files))
        for relative, record in actual_files.items():
            _require_equal(
                f"archived application SHA-256 {relative}",
                _zip_member_sha256(bundled, relative), record["sha256"])
        for relative, expected_hash in source_files.items():
            member = f"_internal/rnastructure-corresponding-source/{relative}"
            if member not in archive_names:
                raise ValueError(
                    f"archive lacks corresponding source member {relative}")
            _require_equal(f"archived source SHA-256 {relative}",
                           _zip_member_sha256(bundled, member), expected_hash)
        _require_equal(
            "archived corresponding-source manifest",
            bundled.read(
                f"_internal/rnastructure-corresponding-source/{source_manifest_path.name}"),
            source_manifest_path.read_bytes(),
        )


def _verify_package_evidence(
    paths: _PackageVerificationPaths,
    release: dict[str, object],
    internal_dir: Path,
) -> tuple[dict[str, set[str]], dict[str, dict[str, set[str]]],
           dict[str, object], dict[str, object]]:
    """Verify exclusion policy, protected payload, licenses, and SPDX evidence."""

    layers = _packaged_layer_inventory(paths.executable, paths.application_dir)
    findings = _forbidden_payload_findings(
        layers, paths.project_root / INVENTORY_RELATIVE_PATH)
    _require_no_forbidden_payload(findings)
    protected = _protected_payload_identity(paths.application_dir)
    protected.update(_allowlisted_payload_identity(
        paths.application_dir, paths.project_root / INVENTORY_RELATIVE_PATH))
    release_evidence = _release_evidence_verifier(paths.project_root)(
        paths.project_root, paths.application_dir, internal_dir)
    inventory = _load_json(paths.project_root / INVENTORY_RELATIVE_PATH)
    _require_no_excluded_evidence(release_evidence, inventory)
    evidence_record = release.get("release_evidence")
    if not isinstance(evidence_record, dict):
        raise ValueError("release manifest lacks license/SBOM evidence identity")
    expected_evidence = {
        "license_manifest": "_internal/LICENSE-MANIFEST.json",
        "license_manifest_sha256": _sha256(internal_dir / "LICENSE-MANIFEST.json"),
        "sbom": "_internal/SBOM.spdx.json",
        "sbom_sha256": _sha256(internal_dir / "SBOM.spdx.json"),
        "license_directory": "_internal/licenses",
        "component_count": len(release_evidence["components"]),
        "license_file_count": len(release_evidence["license_files"]),
    }
    _require_equal("release license/SBOM evidence identity", evidence_record,
                   expected_evidence)
    return layers, findings, protected, release_evidence


def _package_runtime_probe(
    paths: _PackageVerificationPaths,
    provenance: dict[str, object],
    executable_hash: str,
    *,
    run_probes: bool,
) -> dict[str, object]:
    """Run the frozen self-test probe or record its explicit omission."""

    if not run_probes:
        return {"result": "not_run"}
    return _verify_self_test_identity(
        paths.executable, paths.application_dir, provenance, executable_hash)


def _write_package_verification_record(
    paths: _PackageVerificationPaths,
    provenance: dict[str, object],
    hashes: _PackageVerificationHashes,
    source_files: dict[str, str],
    maintained_modules,
    layers: dict[str, set[str]],
    findings: dict[str, dict[str, set[str]]],
    protected: dict[str, object],
    release_evidence: dict[str, object],
    self_test: dict[str, object],
) -> None:
    """Assemble and write the verification evidence record as the final stage."""

    result = self_test.get("result")
    if result not in ("passed", "not_run"):
        raise ValueError("Package verification requires successful or explicitly skipped runtime probes")
    inventory = _load_json(paths.project_root / INVENTORY_RELATIVE_PATH)
    verification_record = {
        "schema_version": 1,
        "application_version": provenance["application_version"],
        "release_mode": paths.release_mode,
        "executable_sha256": hashes.executable,
        "source_file_count": len(source_files),
        "maintained_modules": maintained_modules,
        "layer_counts": {layer: len(entries) for layer, entries in layers.items()},
        "excluded_families": {
            family: {
                "enforced_layers": list(
                    inventory["clean_build_exclusions"]["families"][family][
                        "enforced_layers"]),
                "counts": {
                    layer: len(entries) for layer, entries in layer_hits.items()
                },
            }
            for family, layer_hits in findings.items()
        },
        "protected_payload": protected,
        "release_evidence": {
            "component_count": len(release_evidence["components"]),
            "license_file_count": len(release_evidence["license_files"]),
        },
        "self_test": self_test,
    }
    # Fixture/static checks must never replace final runtime-success evidence.
    suffix = "packaged-verification" if result == "passed" else "packaged-static-verification"
    record_path = paths.application_dir.parent / f"{paths.stem}.{suffix}.json"
    record_path.write_text(
        json.dumps(verification_record, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="ascii", newline="\n")


def verify_packaged_identity(
    project_root: Path = PROJECT_ROOT,
    provenance_path: Path = PROVENANCE_PATH,
    application_dir: Path | None = None,
    archive: Path | None = None,
    release_manifest_path: Path | None = None,
    signing_attestation_path: Path | None = None,
    release_mode: str = "unsigned",
    run_probes: bool = True,
) -> None:
    """Verify the post-sign onedir release, archive, trust, and runtime probes."""
    provenance = verify_source_identity(project_root, provenance_path)
    paths = _package_verification_paths(
        project_root, provenance_path, application_dir, archive,
        release_manifest_path, signing_attestation_path, release_mode,
        str(provenance["application_version"]))
    _verify_release_root_topology(paths.application_dir)
    release = _load_json(paths.release_manifest_path)
    attestation = _load_json(paths.signing_attestation_path)
    hashes = _verify_package_manifest_identity(
        paths, provenance, release, attestation)
    executable_hash = hashes.executable

    actual_files, internal_dir = _verify_package_application_folder(
        paths, release, hashes)
    source_files, maintained_modules, source_manifest_path = (
        _verify_package_corresponding_source(paths, release, internal_dir))
    _verify_package_archive(
        paths, actual_files, source_files, source_manifest_path)
    layers, findings, protected, release_evidence = _verify_package_evidence(
        paths, release, internal_dir)
    self_test = _package_runtime_probe(
        paths, provenance, executable_hash, run_probes=run_probes)
    _write_package_verification_record(
        paths, provenance, hashes, source_files, maintained_modules,
        layers, findings, protected, release_evidence, self_test)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("source", "package"))
    parser.add_argument("--mode", choices=tuple(TRUST_CLASSIFICATIONS), default="unsigned")
    parser.add_argument("--skip-runtime-probes", action="store_true",
                        help="For fixture/unit tests only; authoritative builds must not use this.")
    parser.add_argument("--diagnostics-json", type=Path,
                        help="Explicit local output for full mismatch values; may contain file contents.")
    args = parser.parse_args(argv)
    try:
        if args.phase == "source":
            verify_source_identity()
        else:
            verify_packaged_identity(
                release_mode=args.mode, run_probes=not args.skip_runtime_probes)
    except (OSError, ValueError, zipfile.BadZipFile, subprocess.SubprocessError,
            json.JSONDecodeError) as error:
        if args.diagnostics_json is not None and isinstance(error, IdentityMismatch):
            try:
                # Exclusive creation avoids overwriting source or previous evidence.
                with args.diagnostics_json.open("x", encoding="utf-8") as handle:
                    json.dump({"label": error.label, "expected": error.expected,
                               "actual": error.actual}, handle, indent=2,
                              default=_diagnostic_json_default)
            except (OSError, TypeError, ValueError) as diagnostic_error:
                print(f"Could not write mismatch diagnostics: {diagnostic_error}", file=sys.stderr)
        message = str(error)
        if len(message) > 4096:
            message = message[:4096] + "... [truncated]"
        print(f"Release identity verification failed: {message}", file=sys.stderr)
        return 1
    print(f"Release {args.phase} identity verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
