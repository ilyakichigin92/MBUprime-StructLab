"""Generate and verify offline license evidence for the built Windows application."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import sys
from typing import Iterable


CONFIG_RELATIVE = Path("packaging/release_components.json")
GENERATOR_RELATIVE = Path("packaging/generate_release_evidence.py")
LOCK_RELATIVE = Path("requirements-lock.txt")
MANIFEST_NAME = "LICENSE-MANIFEST.json"
SBOM_NAME = "SBOM.spdx.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _load_object(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return payload


def _locked_versions(path: Path) -> dict[str, str]:
    versions: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"([A-Za-z0-9_.-]+)==([^\s;]+)\s+--hash=sha256:", line)
        if not match:
            raise ValueError(f"unsupported runtime lock entry: {line}")
        versions[_canonical_name(match.group(1))] = match.group(2)
    if not versions:
        raise ValueError("runtime lock contains no distributions")
    return versions


def _pyz_roots(executable: Path) -> set[str]:
    from PyInstaller.archive.readers import CArchiveReader

    archive = CArchiveReader(str(executable))
    names = archive.open_embedded_archive("PYZ.pyz").toc
    return {name.split(".", 1)[0] for name in names}


def _third_party_roots(
    pyz_roots: Iterable[str],
    first_party_roots: Iterable[str],
    packager_roots: Iterable[str],
) -> set[str]:
    excluded = set(sys.stdlib_module_names)
    excluded.update(first_party_roots)
    excluded.update(packager_roots)
    return {root for root in pyz_roots if root not in excluded}


def _distribution_license_sources(distribution: metadata.Distribution) -> list[Path]:
    sources: list[Path] = []
    for relative in distribution.files or ():
        name = PurePosixPath(str(relative)).name.lower()
        if not any(token in name for token in ("license", "copying", "notice")):
            continue
        source = Path(distribution.locate_file(relative)).resolve()
        if source.is_file():
            sources.append(source)
    return sorted(set(sources), key=lambda path: path.as_posix().lower())


def _copy_evidence(
    source: Path,
    destination: Path,
    source_locator: str,
    component_id: str,
) -> dict[str, object]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    return {
        "path": destination.as_posix(),
        "sha256": sha256(destination),
        "size": destination.stat().st_size,
        "source": source_locator,
        "components": [component_id],
    }


def _distribution_component(
    name: str,
    module_roots: list[str],
    metadata_dirs: list[str],
    output_dir: Path,
    license_override: str | None = None,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    distribution = metadata.distribution(name)
    canonical = _canonical_name(distribution.metadata["Name"] or name)
    version = distribution.version
    component_id = f"python-{canonical}"
    sources = _distribution_license_sources(distribution)
    if not sources:
        raise ValueError(f"distribution has no authoritative local license payload: {name}")
    records: list[dict[str, object]] = []
    for index, source in enumerate(sources, start=1):
        target_name = source.name if len(sources) == 1 else f"{index:02d}-{source.name}"
        relative = Path("licenses") / component_id / target_name
        records.append(_copy_evidence(
            source,
            output_dir / relative,
            f"installed-distribution:{canonical}/{source.name}",
            component_id,
        ) | {"path": relative.as_posix()})
    license_expression = (license_override
                          or distribution.metadata.get("License-Expression")
                          or "NOASSERTION")
    component = {
        "id": component_id,
        "name": distribution.metadata["Name"] or name,
        "version": version,
        "kind": "python-distribution",
        "license_declared": license_expression,
        "top_level_modules": sorted(module_roots),
        "distribution_metadata": sorted(metadata_dirs),
        "license_paths": [record["path"] for record in records],
    }
    return component, records


def _static_sources(
    project_root: Path,
    spec: dict[str, object],
) -> list[tuple[Path, str]]:
    source = spec["license_source"]
    kind = source["kind"]
    if kind == "project":
        return [
            (project_root / relative, f"project:{relative}")
            for relative in source["paths"]
        ]
    if kind == "python-root":
        relative = source["path"]
        return [(Path(sys.base_prefix) / relative, f"python-runtime:{relative}")]
    if kind == "python-tcl":
        relative = source["path"]
        return [(Path(sys.base_prefix) / "tcl" / relative, f"python-tcl:{relative}")]
    if kind == "distribution":
        distribution = metadata.distribution(source["name"])
        return [
            (path, f"installed-distribution:{_canonical_name(source['name'])}/{path.name}")
            for path in _distribution_license_sources(distribution)
        ]
    raise ValueError(f"unsupported static license source kind: {kind}")


def _static_payload_matches(
    application_dir: Path,
    spec: dict[str, object],
    component_id: str,
) -> list[str]:
    matches = sorted({
        path.relative_to(application_dir).as_posix()
        for path in application_dir.rglob("*") if path.is_file()
        for pattern in spec["payload_any"]
        if fnmatch.fnmatch(path.relative_to(application_dir).as_posix(), pattern)
    })
    if not matches:
        raise ValueError(f"static component payload is absent: {component_id}")
    return matches


def _copy_static_license_evidence(
    project_root: Path,
    spec: dict[str, object],
    output_dir: Path,
    component_id: str,
) -> list[dict[str, object]]:
    sources = _static_sources(project_root, spec)
    if not sources or any(not path.is_file() for path, _ in sources):
        raise ValueError(f"static component license payload is absent: {component_id}")
    records: list[dict[str, object]] = []
    for index, (source, locator) in enumerate(sources, start=1):
        target_name = source.name if len(sources) == 1 else f"{index:02d}-{source.name}"
        relative = Path("licenses") / component_id / target_name
        records.append(_copy_evidence(
            source, output_dir / relative, locator, component_id,
        ) | {"path": relative.as_posix()})
    return records


def _static_component_version(spec: dict[str, object]) -> str:
    if spec.get("version_source") == "python":
        return platform.python_version()
    if str(spec.get("version_source", "")).startswith("distribution:"):
        return metadata.version(str(spec["version_source"]).split(":", 1)[1])
    if spec.get("version_source") == "numpy-build-config:blas":
        import numpy

        build = getattr(numpy.__config__, "CONFIG", {}).get(
            "Build Dependencies", {}).get("blas", {})
        blas_version = str(build.get("version", ""))
        if (not blas_version or build.get("found") is not True
                or "openblas" not in str(build.get("name", "")).lower()):
            raise ValueError("NumPy build config has no verified OpenBLAS version")
        return blas_version
    return str(spec.get("version", ""))


def _static_component(
    project_root: Path,
    application_dir: Path,
    spec: dict[str, object],
    output_dir: Path,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    component_id = str(spec["id"])
    matches = _static_payload_matches(application_dir, spec, component_id)
    records = _copy_static_license_evidence(
        project_root, spec, output_dir, component_id)
    version = _static_component_version(spec)
    component = {
        "id": component_id,
        "name": spec["name"],
        "version": version,
        "kind": "native-or-data-component",
        "license_declared": str(spec.get("license_declared", "NOASSERTION")),
        "payload_matches": matches,
        "license_paths": [record["path"] for record in records],
    }
    return component, records


def _physical_metadata_dirs(application_dir: Path) -> set[str]:
    internal = application_dir / "_internal"
    return {
        path.name for path in internal.iterdir()
        if path.is_dir() and path.name.lower().endswith(".dist-info")
    }


def _installed_metadata_dir(distribution: metadata.Distribution) -> str:
    path = getattr(distribution, "_path", None)
    return Path(path).name if path is not None else ""


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"


def _spdx_document(
    application_version: str,
    build_utc: str,
    components: list[dict[str, object]],
    application_license: str,
) -> dict[str, object]:
    component_digest = hashlib.sha256(_canonical_json(components).encode("ascii")).hexdigest()
    application_id = "SPDXRef-Application"
    packages = [{
        "SPDXID": application_id,
        "name": "MBUprime StructLab",
        "versionInfo": application_version,
        "downloadLocation": "NOASSERTION",
        "filesAnalyzed": False,
        "licenseConcluded": application_license,
        "licenseDeclared": application_license,
        "copyrightText": "NOASSERTION",
    }]
    relationships: list[dict[str, str]] = []
    for component in components:
        package_id = "SPDXRef-" + re.sub(r"[^A-Za-z0-9.-]", "-", str(component["id"]))
        packages.append({
            "SPDXID": package_id,
            "name": component["name"],
            "versionInfo": component["version"],
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "licenseConcluded": "NOASSERTION",
            "licenseDeclared": component["license_declared"],
            "copyrightText": "NOASSERTION",
        })
        relationships.append({
            "spdxElementId": application_id,
            "relationshipType": "DEPENDS_ON",
            "relatedSpdxElement": package_id,
        })
    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": "MBUprime-StructLab-Windows-release",
        "documentNamespace": f"https://github.com/ilyakichigin92/MBUprime-StructLab/sbom/{component_digest}",
        "creationInfo": {
            "created": build_utc,
            "creators": ["Tool: packaging/generate_release_evidence.py"],
        },
        "packages": packages,
        "relationships": relationships,
        "documentDescribes": [application_id],
    }


def _prepare_release_evidence_output(output_dir: Path) -> None:
    licenses_dir = output_dir / "licenses"
    if licenses_dir.exists():
        shutil.rmtree(licenses_dir)
    for name in (MANIFEST_NAME, SBOM_NAME):
        (output_dir / name).unlink(missing_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)


def _runtime_distributions(
        pyz_roots: set[str], config: dict[str, object]
) -> tuple[set[str], dict[str, set[str]], list[str]]:
    third_party_roots = _third_party_roots(
        pyz_roots, config["first_party_module_roots"],
        config["packager_module_roots"])
    package_map = metadata.packages_distributions()
    roots_by_distribution: dict[str, set[str]] = {}
    unclassified_roots: list[str] = []
    for root in sorted(third_party_roots):
        distributions = package_map.get(root, ())
        if not distributions:
            unclassified_roots.append(root)
            continue
        for distribution_name in distributions:
            roots_by_distribution.setdefault(
                _canonical_name(distribution_name), set()).add(root)
    for distribution_name in config["always_include_distributions"]:
        roots_by_distribution.setdefault(_canonical_name(distribution_name), set())
    return third_party_roots, roots_by_distribution, unclassified_roots


def _distribution_metadata_matches(
        application_dir: Path, roots_by_distribution: dict[str, set[str]]
) -> tuple[list[str], dict[str, list[str]], set[str]]:
    physical_metadata = _physical_metadata_dirs(application_dir)
    metadata_by_distribution: dict[str, list[str]] = {}
    unclassified_metadata = set(physical_metadata)
    for canonical in roots_by_distribution:
        distribution = metadata.distribution(canonical)
        installed_dir = _installed_metadata_dir(distribution)
        matches = [
            name for name in physical_metadata
            if name.lower() == installed_dir.lower()]
        metadata_by_distribution[canonical] = matches
        unclassified_metadata.difference_update(matches)
    return physical_metadata, metadata_by_distribution, unclassified_metadata


def _collect_release_components(
        project_root: Path, application_dir: Path, output_dir: Path,
        config: dict[str, object], locked: dict[str, str],
        roots_by_distribution: dict[str, set[str]],
        metadata_by_distribution: dict[str, list[str]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    components: list[dict[str, object]] = []
    license_records: list[dict[str, object]] = []
    for canonical in sorted(roots_by_distribution):
        component, records = _distribution_component(
            canonical, sorted(roots_by_distribution[canonical]),
            metadata_by_distribution.get(canonical, []), output_dir,
            str(config.get("distribution_license_overrides", {}).get(
                canonical, "")) or None)
        if canonical in locked and component["version"] != locked[canonical]:
            raise ValueError(
                f"locked component version mismatch for {canonical}: "
                f"expected {locked[canonical]}, found {component['version']}")
        component["lock_status"] = (
            "locked-runtime" if canonical in locked
            else "observed-build-environment")
        components.append(component)
        license_records.extend(records)
    for spec in config["static_components"]:
        component, records = _static_component(
            project_root, application_dir, spec, output_dir)
        components.append(component)
        license_records.extend(records)
    components.sort(key=lambda item: str(item["id"]))
    license_records.sort(key=lambda item: str(item["path"]))
    return components, license_records


def generate_release_evidence(
    project_root: Path,
    application_dir: Path,
    output_dir: Path,
) -> dict[str, object]:
    """Generate evidence from the application that PyInstaller actually emitted."""
    project_root = project_root.resolve()
    application_dir = application_dir.resolve()
    output_dir = output_dir.resolve()
    executable = application_dir / "MBUprime StructLab.exe"
    if not executable.is_file() or not (application_dir / "_internal").is_dir():
        raise ValueError(f"built onedir application is incomplete: {application_dir}")
    config_path = project_root / CONFIG_RELATIVE
    lock_path = project_root / LOCK_RELATIVE
    config = _load_object(config_path)
    locked = _locked_versions(lock_path)
    provenance = _load_object(project_root / "packaging/release_provenance.json")
    _prepare_release_evidence_output(output_dir)
    pyz_roots = _pyz_roots(executable)
    third_party_roots, roots_by_distribution, unclassified_roots = (
        _runtime_distributions(pyz_roots, config))
    physical_metadata, metadata_by_distribution, unclassified_metadata = (
        _distribution_metadata_matches(application_dir, roots_by_distribution))
    components, license_records = _collect_release_components(
        project_root, application_dir, output_dir, config, locked,
        roots_by_distribution, metadata_by_distribution)
    manifest = {
        "schema_version": 1,
        "target": "cp312-windows-x86_64",
        "application_version": provenance["application_version"],
        "inputs": {
            CONFIG_RELATIVE.as_posix(): sha256(config_path),
            GENERATOR_RELATIVE.as_posix(): sha256(project_root / GENERATOR_RELATIVE),
            LOCK_RELATIVE.as_posix(): sha256(lock_path),
        },
        "observed_pyz_top_level_modules": sorted(pyz_roots),
        "observed_third_party_top_level_modules": sorted(third_party_roots),
        "observed_distribution_metadata": sorted(physical_metadata),
        "unclassified_top_level_modules": sorted(unclassified_roots),
        "unclassified_distribution_metadata": sorted(unclassified_metadata),
        "components": components,
        "license_files": license_records,
    }
    if unclassified_roots or unclassified_metadata:
        raise ValueError(
            "release evidence has unclassified payload: "
            f"modules={unclassified_roots}, metadata={sorted(unclassified_metadata)}")

    (output_dir / MANIFEST_NAME).write_text(
        _canonical_json(manifest), encoding="ascii", newline="\n")
    sbom = _spdx_document(
        str(provenance["application_version"]),
        str(provenance["build_utc"]),
        components,
        str(config["application_license"]),
    )
    (output_dir / SBOM_NAME).write_text(
        _canonical_json(sbom), encoding="ascii", newline="\n")
    return manifest


def _verify_release_evidence_inputs(
    project_root: Path, evidence_dir: Path,
) -> tuple[dict[str, object], dict[str, object]]:
    """Validate evidence schema and the source inputs that generated it."""

    manifest = _load_object(evidence_dir / MANIFEST_NAME)
    config = _load_object(project_root / CONFIG_RELATIVE)
    if manifest.get("schema_version") != 1:
        raise ValueError("release license manifest schema mismatch")
    expected_inputs = {
        CONFIG_RELATIVE.as_posix(): sha256(project_root / CONFIG_RELATIVE),
        GENERATOR_RELATIVE.as_posix(): sha256(project_root / GENERATOR_RELATIVE),
        LOCK_RELATIVE.as_posix(): sha256(project_root / LOCK_RELATIVE),
    }
    if manifest.get("inputs") != expected_inputs:
        raise ValueError("release license manifest source inputs are stale")
    return manifest, config


def _verify_release_evidence_observations(
    application_dir: Path,
    manifest: dict[str, object],
    config: dict[str, object],
) -> tuple[set[str], set[str]]:
    """Recompute packaged module and distribution observations."""

    pyz_roots = _pyz_roots(application_dir / "MBUprime StructLab.exe")
    third_party_roots = _third_party_roots(
        pyz_roots,
        config["first_party_module_roots"],
        config["packager_module_roots"],
    )
    if manifest.get("observed_pyz_top_level_modules") != sorted(pyz_roots):
        raise ValueError("release license manifest PYZ observation is stale")
    if manifest.get("observed_third_party_top_level_modules") != sorted(third_party_roots):
        raise ValueError("release license manifest third-party classification is stale")
    physical_metadata = _physical_metadata_dirs(application_dir)
    if manifest.get("observed_distribution_metadata") != sorted(physical_metadata):
        raise ValueError("release license manifest distribution metadata observation is stale")
    if manifest.get("unclassified_top_level_modules") or manifest.get("unclassified_distribution_metadata"):
        raise ValueError("release license manifest contains unclassified payload")
    return third_party_roots, physical_metadata


def _verify_release_evidence_inventory(
    manifest: dict[str, object],
    third_party_roots: set[str],
    physical_metadata: set[str],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Validate component classification and license-record inventory."""

    components = manifest.get("components")
    records = manifest.get("license_files")
    if not isinstance(components, list) or not components or not isinstance(records, list) or not records:
        raise ValueError("release license manifest inventory is empty")
    declared_roots = {
        root for component in components
        for root in component.get("top_level_modules", ())
    }
    if declared_roots != third_party_roots:
        raise ValueError("release license manifest leaves third-party modules unclassified")
    declared_metadata = {
        name for component in components
        for name in component.get("distribution_metadata", ())
    }
    if declared_metadata != physical_metadata:
        raise ValueError("release license manifest leaves distribution metadata unclassified")
    return components, records


def _verify_release_license_payload(
    evidence_dir: Path, records: list[dict[str, object]],
) -> None:
    """Validate the complete staged license payload against its manifest."""

    expected_license_paths: set[str] = set()
    for record in records:
        relative = str(record["path"])
        if not relative.startswith("licenses/") or ".." in PurePosixPath(relative).parts:
            raise ValueError(f"invalid release license path: {relative}")
        path = evidence_dir / PurePosixPath(relative)
        if not path.is_file():
            raise ValueError(f"release license payload is absent: {relative}")
        if sha256(path) != record.get("sha256") or path.stat().st_size != record.get("size"):
            raise ValueError(f"release license payload was mutated: {relative}")
        expected_license_paths.add(relative)
    actual_license_paths = {
        path.relative_to(evidence_dir).as_posix()
        for path in (evidence_dir / "licenses").rglob("*") if path.is_file()
    }
    if actual_license_paths != expected_license_paths:
        raise ValueError("release license payload set differs from its manifest")


def _verify_release_sbom(
    evidence_dir: Path, components: list[dict[str, object]],
) -> None:
    """Validate SPDX identity and component parity with the license manifest."""

    sbom = _load_object(evidence_dir / SBOM_NAME)
    if sbom.get("spdxVersion") != "SPDX-2.3" or sbom.get("dataLicense") != "CC0-1.0":
        raise ValueError("release SBOM is not SPDX 2.3 JSON")
    sbom_components = {
        package["SPDXID"].removeprefix("SPDXRef-")
        for package in sbom.get("packages", ())
        if package.get("SPDXID") != "SPDXRef-Application"
    }
    manifest_components = {
        re.sub(r"[^A-Za-z0-9.-]", "-", str(component["id"]))
        for component in components
    }
    if sbom_components != manifest_components:
        raise ValueError("release SBOM component set differs from license manifest")


def verify_release_evidence(
    project_root: Path,
    application_dir: Path,
    evidence_dir: Path | None = None,
) -> dict[str, object]:
    """Fail closed when packaged evidence is missing, stale, mutated, or unclassified."""
    project_root = project_root.resolve()
    application_dir = application_dir.resolve()
    evidence_dir = (evidence_dir or application_dir / "_internal").resolve()
    manifest, config = _verify_release_evidence_inputs(
        project_root, evidence_dir)
    third_party_roots, physical_metadata = (
        _verify_release_evidence_observations(
            application_dir, manifest, config))
    components, records = _verify_release_evidence_inventory(
        manifest, third_party_roots, physical_metadata)
    _verify_release_license_payload(evidence_dir, records)
    _verify_release_sbom(evidence_dir, components)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--application-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)
    application = args.application_dir or args.project_root / "dist" / "MBUprime StructLab"
    output = args.output_dir or application / "_internal"
    try:
        if args.verify:
            verify_release_evidence(args.project_root, application, output)
            print("Release license manifest and SPDX SBOM verified.")
        else:
            manifest = generate_release_evidence(args.project_root, application, output)
            print(
                f"Generated release evidence for {len(manifest['components'])} components "
                f"and {len(manifest['license_files'])} license files.")
    except (OSError, ValueError, json.JSONDecodeError, metadata.PackageNotFoundError) as error:
        print(f"Release evidence failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
