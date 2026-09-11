"""Reproducibility metadata and numeric boundary helpers.

This module is intentionally dependency-light.  It defines the public
scientific policy/version contract, typed diagnostics, deterministic decimal
quantisation, and the canonical manifest used by every report export.
"""

from __future__ import annotations

import hashlib
import copy
import importlib
import importlib.metadata
import json
import math
import os
import platform
import re
import sys
from dataclasses import dataclass, field, fields
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Mapping, Sequence


APPLICATION_VERSION = "2.4.4"
APP_VERSION = APPLICATION_VERSION
SCIENTIFIC_POLICY_VERSION = "2026-09-08-context-retention-and-duplex-offsets-1"
MANIFEST_SCHEMA_VERSION = "7"
ANALYSIS_SCHEMA_VERSION = MANIFEST_SCHEMA_VERSION
VIENNA_PARAMETER_SET = "DNA_Mathews2004"
REQUIRED_PRIMER3_PY_VERSION = "2.3.0"
REQUIRED_LIBPRIMER3_VERSION = "2.6.1"
REQUIRED_VIENNARNA_VERSION = "2.7.2"
SALT_FORMULA_ID = "na_equiv_owczarzy_120_sqrt_free_mg_v1"
DG_DECISION_RESOLUTION = Decimal("0.1")
TM_DECISION_RESOLUTION = Decimal("0.1")
# setup.py replaces this exact sentinel only in the wheel build directory.
# A repository checkout deliberately retains the empty value and computes its
# identity from the live release inventory instead.
_BUNDLED_SOURCE_PROVENANCE_JSON = "{}"


@dataclass(frozen=True)
class ScientificDiagnostic:
    """One typed, serialisable calculation diagnostic."""

    code: str
    engine: str
    quantity: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "engine": self.engine,
            "quantity": self.quantity,
            "message": self.message,
        }


def quantize_decimal(value: object, resolution: Decimal) -> Decimal:
    """Quantise a finite number using decimal, half-away-from-zero policy."""

    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("value must be numeric") from exc
    if not result.is_finite():
        raise ValueError("value must be finite")
    return result.quantize(resolution, rounding=ROUND_HALF_UP)


def finite_or_none(
    value: object,
    *,
    engine: str,
    quantity: str,
    diagnostics: list[ScientificDiagnostic] | None = None,
    none_is_diagnostic: bool = True,
) -> float | None:
    """Normalise an engine numeric result and record why it was discarded."""

    code = ""
    message = ""
    if value is None:
        if not none_is_diagnostic:
            return None
        code = "missing_value"
        message = "backend returned no numeric value"
    else:
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            code = "non_numeric_value"
            message = f"backend returned {type(value).__name__}, not a number"
        else:
            if math.isfinite(number):
                return number
            code = "non_finite_value"
            message = "backend returned NaN or infinity"
    if diagnostics is not None:
        diagnostics.append(ScientificDiagnostic(code, engine, quantity, message))
    return None


def exception_diagnostic(engine: str, quantity: str,
                         exc: BaseException) -> ScientificDiagnostic:
    return ScientificDiagnostic(
        "engine_exception",
        engine,
        quantity,
        f"{type(exc).__name__}: {exc}",
    )


@dataclass(frozen=True)
class ScientificManifest:
    schema_version: str
    application_version: str
    scientific_policy_version: str
    python_version: str
    primer3_py_version: str
    primer3_core_version: str
    viennarna_version: str
    vienna_parameter_set: str
    vienna_salt_applied: bool
    operating_system: str
    architecture: str
    source_commit: str
    source_tree_hash: str
    dependency_lock_hash: str
    build_date: str
    release_artifact_hash: str
    max_variants: int
    enumeration_pool: int
    min_sequence_length: int
    max_sequence_length: int
    engine_search_policies: tuple[tuple[str, object], ...]
    final_union_truncated: bool
    degenerate_structure_scope: str
    severity_policy: tuple[tuple[str, object], ...]
    geometry_policy: tuple[tuple[str, object], ...]
    variant_policy: tuple[tuple[str, object], ...]
    dg_decision_resolution: str
    tm_decision_resolution: str
    salt_formula_id: str
    normalized_inputs: tuple[tuple[tuple[str, object], ...], ...]
    conditions: tuple[tuple[str, object], ...]
    diagnostics: tuple[ScientificDiagnostic, ...]
    engines: tuple[tuple[str, object], ...]
    analysis_id: str
    ensemble_plan: tuple[tuple[str, object], ...] = field(default_factory=tuple)
    ensemble_coverage: tuple[tuple[str, object], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        # Frozen dataclasses alone do not detach nested caller-owned containers.
        for name in ("engine_search_policies", "severity_policy", "geometry_policy",
                     "variant_policy", "normalized_inputs", "conditions", "engines",
                     "ensemble_plan", "ensemble_coverage"):
            object.__setattr__(self, name, _freeze_value(getattr(self, name)))

    def to_dict(self, *, include_analysis_id: bool = True,
                include_volatile_build: bool = True) -> dict[str, object]:
        result: dict[str, object] = {
            "schema_version": self.schema_version,
            "application_version": self.application_version,
            "scientific_policy_version": self.scientific_policy_version,
            "python_version": self.python_version,
            "primer3_py_version": self.primer3_py_version,
            "primer3_core_version": self.primer3_core_version,
            "viennarna_version": self.viennarna_version,
            "vienna_parameter_set": self.vienna_parameter_set,
            "vienna_salt_applied": self.vienna_salt_applied,
            "operating_system": self.operating_system,
            "architecture": self.architecture,
            "source_commit": self.source_commit,
            "source_tree_hash": self.source_tree_hash,
            "dependency_lock_hash": self.dependency_lock_hash,
            "max_variants": self.max_variants,
            "enumeration_pool": self.enumeration_pool,
            "min_sequence_length": self.min_sequence_length,
            "max_sequence_length": self.max_sequence_length,
            "engine_search_policies": _thaw_value(self.engine_search_policies),
            "final_union_truncated": self.final_union_truncated,
            "degenerate_structure_scope": self.degenerate_structure_scope,
            "severity_policy": _thaw_value(self.severity_policy),
            "geometry_policy": _thaw_value(self.geometry_policy),
            "variant_policy": _thaw_value(self.variant_policy),
            "dg_decision_resolution": self.dg_decision_resolution,
            "tm_decision_resolution": self.tm_decision_resolution,
            "salt_formula_id": self.salt_formula_id,
            "normalized_inputs": [
                {key: _thaw_value(value) for key, value in item}
                for item in self.normalized_inputs],
            "conditions": {key: _thaw_value(value) for key, value in self.conditions},
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "engines": {
                engine: _thaw_value(metadata)
                for engine, metadata in self.engines
            },
            "ensemble_plan": _thaw_value(self.ensemble_plan),
            "ensemble_coverage": _thaw_value(self.ensemble_coverage),
        }
        if include_volatile_build:
            result["build_date"] = self.build_date
            result["release_artifact_hash"] = self.release_artifact_hash
        if include_analysis_id:
            result["analysis_id"] = self.analysis_id
        return result

    def canonical_json(self) -> str:
        return canonical_json(self.to_dict())


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _frozen_mapping_items(value: object):
    if isinstance(value, _FrozenSequence):
        return None
    if isinstance(value, _FrozenMapping) or (isinstance(value, tuple) and value
            and all(isinstance(item, tuple) and len(item) == 2
                    and isinstance(item[0], str) for item in value)):
        return value
    return None


def _iter_canonical_json(value: object):
    """Yield canonical JSON without constructing a thawed duplicate graph."""

    if isinstance(value, ScientificDiagnostic):
        value = value.to_dict()
    frozen_items = _frozen_mapping_items(value)
    if isinstance(value, Mapping) or frozen_items is not None:
        items = (value.items() if isinstance(value, Mapping)
                 else frozen_items)
        yield "{"
        for index, (key, item) in enumerate(sorted(items)):
            if index:
                yield ","
            yield json.dumps(key, ensure_ascii=False, allow_nan=False)
            yield ":"
            yield from _iter_canonical_json(item)
        yield "}"
        return
    if isinstance(value, (list, tuple)):
        yield "["
        for index, item in enumerate(value):
            if index:
                yield ","
            yield from _iter_canonical_json(item)
        yield "]"
        return
    yield json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False)


def _iter_identity_engines(engines: tuple[tuple[str, object], ...]):
    yield "{"
    for engine_index, (engine, metadata) in enumerate(sorted(engines)):
        if engine_index:
            yield ","
        yield json.dumps(engine, ensure_ascii=False, allow_nan=False)
        yield ":"
        items = _frozen_mapping_items(metadata)
        if items is None:
            yield from _iter_canonical_json(metadata)
            continue
        yield "{"
        retained = sorted(
            (key, value) for key, value in items
            if key not in {"path", "efn2_path"})
        for item_index, (key, value) in enumerate(retained):
            if item_index:
                yield ","
            yield json.dumps(key, ensure_ascii=False, allow_nan=False)
            yield ":"
            yield from _iter_canonical_json(value)
        yield "}"
    yield "}"


def _iter_pair_object(items: tuple[tuple[str, object], ...]):
    yield "{"
    for index, (key, value) in enumerate(sorted(items)):
        if index:
            yield ","
        yield json.dumps(key, ensure_ascii=False, allow_nan=False)
        yield ":"
        yield from _iter_canonical_json(value)
    yield "}"


def iter_analysis_identity_json(manifest: ScientificManifest):
    """Yield the exact canonical analysis-identity JSON in bounded chunks."""

    omitted = {"analysis_id", "build_date", "release_artifact_hash"}
    names = sorted(
        item.name for item in fields(ScientificManifest)
        if item.name not in omitted)
    yield "{"
    for index, name in enumerate(names):
        if index:
            yield ","
        yield json.dumps(name, ensure_ascii=False, allow_nan=False)
        yield ":"
        value = getattr(manifest, name)
        if name == "engines":
            yield from _iter_identity_engines(value)
        elif name == "conditions":
            yield from _iter_pair_object(value)
        else:
            yield from _iter_canonical_json(value)
    yield "}"


def analysis_identity_sha256(manifest: ScientificManifest) -> str:
    """Return the manifest identity without materializing its JSON graph."""

    digest = hashlib.sha256()
    for chunk in iter_analysis_identity_json(manifest):
        digest.update(chunk.encode("utf-8"))
    return digest.hexdigest()


def _package_version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return ""


def validated_primer3_runtime_identity(primer3_module=None) -> dict[str, str]:
    """Return the exact Primer3 binding/core identity or fail preflight."""

    try:
        distribution = importlib.metadata.distribution("primer3-py")
        module = primer3_module or importlib.import_module("primer3")
    except (ImportError, importlib.metadata.PackageNotFoundError) as exc:
        raise RuntimeError("mandatory primer3-py backend is unavailable") from exc
    distribution_version = distribution.version
    module_version = str(getattr(module, "__version__", "") or "")
    if (distribution_version != REQUIRED_PRIMER3_PY_VERSION
            or module_version != REQUIRED_PRIMER3_PY_VERSION):
        raise RuntimeError(
            f"mandatory primer3-py must be {REQUIRED_PRIMER3_PY_VERSION}; "
            f"found distribution={distribution_version or 'unknown'}, "
            f"module={module_version or 'unknown'}")
    about = Path(distribution.locate_file(
        "primer3/src/libprimer3/ABOUT.txt"))
    try:
        about_text = about.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            "mandatory libprimer3 identity evidence is unavailable") from exc
    marker = f"derived from the Primer {REQUIRED_LIBPRIMER3_VERSION}\nsource"
    if marker not in about_text.replace("\r\n", "\n"):
        raise RuntimeError(
            f"mandatory libprimer3 must be {REQUIRED_LIBPRIMER3_VERSION}")
    try:
        live_core = module.thermoanalysis.get_libprimer3_version()
    except (AttributeError, TypeError) as exc:
        raise RuntimeError(
            "mandatory live libprimer3 identity API is unavailable") from exc
    match = re.fullmatch(r"libprimer3 release (\d+\.\d+\.\d+)",
                         str(live_core))
    if match is None or match.group(1) != REQUIRED_LIBPRIMER3_VERSION:
        raise RuntimeError(
            f"mandatory live libprimer3 must be {REQUIRED_LIBPRIMER3_VERSION}; "
            f"found {live_core!r}")
    try:
        from native_target import current_target

        thermoanalysis = Path(module.thermoanalysis.__file__).resolve()
        p3helpers = Path(module.p3helpers.__file__).resolve()
        if not thermoanalysis.is_file() or not p3helpers.is_file():
            raise OSError("native binding file is absent")
    except (AttributeError, OSError, RuntimeError) as exc:
        raise RuntimeError(
            "mandatory primer3 native binding identity is unavailable") from exc
    return {
        "primer3_py_version": distribution_version,
        "libprimer3_version": match.group(1),
        "target": current_target(),
        "thermoanalysis_filename": thermoanalysis.name,
        "thermoanalysis_sha256": _sha256_file(thermoanalysis),
        "p3helpers_filename": p3helpers.name,
        "p3helpers_sha256": _sha256_file(p3helpers),
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validated_vienna_runtime_identity(rna_module=None) -> dict[str, str]:
    """Return the exact ViennaRNA Python/native-binding identity."""

    try:
        module = rna_module or importlib.import_module("RNA")
        installed = str(module.__version__)
        native = Path(module._RNA.__file__).resolve()
        if not native.is_file():
            raise OSError("native binding file is absent")
        from native_target import current_target
    except (ImportError, AttributeError, OSError, RuntimeError) as exc:
        raise RuntimeError(
            "mandatory ViennaRNA native binding identity is unavailable") from exc
    if installed != REQUIRED_VIENNARNA_VERSION:
        raise RuntimeError(
            f"mandatory ViennaRNA must be exactly {REQUIRED_VIENNARNA_VERSION}; "
            f"found {installed or 'unknown'}")
    return {
        "version": installed,
        "target": current_target(),
        "native_binding_filename": native.name,
        "native_binding_sha256": _sha256_file(native),
    }


def _scientific_inventory_files(root: Path) -> tuple[str, ...]:
    inventory_path = root / "packaging" / "release_inventory.json"
    inventory = json.loads(inventory_path.read_text(encoding="ascii"))
    target = inventory["targets"]["scientific_identity"]
    files: set[str] = set()
    for group_name in target["groups"]:
        files.update(inventory["source_groups"][group_name].get("files", ()))
    try:
        from native_target import current_target
        generated_name = target.get(
            "generated_artifacts_by_target", {}).get(current_target())
    except RuntimeError:
        generated_name = None
    if generated_name is not None:
        files.add(inventory["generated_artifacts"][generated_name]["path"])
    return tuple(sorted(files))


def compute_source_tree_hash(
    root: Path,
    *,
    overrides: Mapping[str, Path] | None = None,
) -> str:
    """Hash the declared scientific source set under *root* canonically.

    ``overrides`` is used only by wheel construction for a target-qualified
    native manifest generated into the build directory before Python modules
    are copied. Source checkouts always hash their live declared files.
    """

    digest = hashlib.sha256()
    for name in _scientific_inventory_files(root):
        path = (overrides or {}).get(name, root / name)
        if not path.is_file():
            raise FileNotFoundError(f"declared scientific source is absent: {name}")
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _bundled_source_provenance() -> dict[str, object]:
    try:
        payload = json.loads(_BUNDLED_SOURCE_PROVENANCE_JSON)
    except json.JSONDecodeError as exc:  # pragma: no cover - build corruption
        raise RuntimeError("bundled scientific provenance is invalid") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("bundled scientific provenance has invalid shape")
    return payload


def _is_source_checkout(root: Path) -> bool:
    return (root / "packaging" / "release_inventory.json").is_file()


def _provenance_candidates() -> tuple[Path, ...]:
    candidates: list[Path] = []
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        candidates.append(Path(bundle_root) / "release_provenance.json")
    if _is_frozen():
        candidates.append(Path(sys.executable).resolve().parent /
                          "release_provenance.json")
    candidates.append(Path(__file__).resolve().parent / "release_provenance.json")
    return tuple(dict.fromkeys(candidates))


@lru_cache(maxsize=1)
def release_provenance() -> dict[str, object]:
    """Read frozen-release or wheel-build provenance when available."""

    for path in _provenance_candidates():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return payload
    return _bundled_source_provenance()


def _nested_mapping(payload: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = payload.get(key, {})
    return value if isinstance(value, Mapping) else {}


def _provenance_text(payload: Mapping[str, object], key: str,
                     fallback: str = "") -> str:
    value = payload.get(key)
    return str(value) if value is not None else fallback


@lru_cache(maxsize=1)
def dependency_lock_hash() -> str:
    root = Path(__file__).resolve().parent
    path = root / "requirements-lock.txt"
    if not _is_frozen() and _is_source_checkout(root):
        if not path.is_file():
            raise RuntimeError("source checkout lacks requirements-lock.txt")
        return _sha256_file(path)
    value = _provenance_text(
        release_provenance(), "requirements_lock_sha256")
    if not value:
        raise RuntimeError("installed distribution lacks dependency-lock provenance")
    return value


@lru_cache(maxsize=1)
def source_tree_hash() -> str:
    root = Path(__file__).resolve().parent
    if not _is_frozen() and _is_source_checkout(root):
        return compute_source_tree_hash(root)
    value = _provenance_text(release_provenance(), "source_tree_sha256")
    if not value:
        raise RuntimeError("installed distribution lacks scientific source provenance")
    return value


def _source_commit() -> str:
    root = Path(__file__).resolve().parent
    head = root / ".git" / "HEAD"
    if not _is_frozen() and _is_source_checkout(root):
        try:
            value = head.read_text(encoding="utf-8").strip()
            if value.startswith("ref: "):
                value = (root / ".git" / value[5:]).read_text(
                    encoding="utf-8").strip()
            return value
        except OSError:
            return ""
    return _provenance_text(release_provenance(), "source_commit")


@lru_cache(maxsize=1)
def release_artifact_hash() -> str:
    if not _is_frozen():
        return ""
    executable = Path(sys.executable)
    return _sha256_file(executable) if executable.is_file() else ""


def _ordered_mapping(values: Mapping[str, object]) -> tuple[tuple[str, object], ...]:
    return tuple((key, values[key]) for key in sorted(values))


class _FrozenMapping(tuple):
    """Immutable mapping marker, including the otherwise ambiguous empty map."""


class _FrozenSequence(tuple):
    """Immutable JSON array, including arrays whose items resemble map entries."""


def _freeze_value(value: object) -> object:
    if isinstance(value, (_FrozenMapping, _FrozenSequence)):
        return value
    if isinstance(value, Mapping):
        return _FrozenMapping((key, _freeze_value(value[key])) for key in sorted(value))
    if isinstance(value, list):
        return _FrozenSequence(_freeze_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_value(item) for item in value)
    return value


def _thaw_value(value: object) -> object:
    if isinstance(value, _FrozenSequence):
        return [_thaw_value(item) for item in value]
    if isinstance(value, _FrozenMapping) or (isinstance(value, tuple) and value
            and all(isinstance(item, tuple) and len(item) == 2
                    and isinstance(item[0], str) for item in value)):
        return {key: _thaw_value(item) for key, item in value}
    if isinstance(value, tuple):
        return [_thaw_value(item) for item in value]
    return copy.deepcopy(value)


def build_manifest(
    *,
    normalized_inputs: Sequence[Mapping[str, object]],
    conditions: Mapping[str, object],
    diagnostics: Iterable[ScientificDiagnostic],
    primer3_py_version: str = "",
    primer3_core_version: str = "",
    vienna_version: str = "",
    vienna_salt_applied: bool,
    max_variants: int,
    enumeration_pool: int,
    min_sequence_length: int,
    max_sequence_length: int,
    engine_search_policies: Mapping[str, object],
    final_union_truncated: bool,
    degenerate_structure_scope: str,
    severity_policy: Mapping[str, object],
    geometry_policy: Mapping[str, object],
    variant_policy: Mapping[str, object],
    engines: Mapping[str, object] | None = None,
    ensemble_plan: Mapping[str, object] | None = None,
    ensemble_coverage: Mapping[str, object] | None = None,
) -> ScientificManifest:
    """Build a deterministic scientific manifest from the supplied analysis inputs."""

    provenance = release_provenance()
    runtime = _nested_mapping(provenance, "runtime")
    dependencies = _nested_mapping(provenance, "dependencies")
    common = dict(
        # Checked-in release provenance describes the last built artifact.
        # Source analyses use the current schema/policy constants; a later
        # release rebuild will generate matching provenance atomically.
        schema_version=MANIFEST_SCHEMA_VERSION,
        application_version=_provenance_text(
            provenance, "application_version", APPLICATION_VERSION),
        scientific_policy_version=SCIENTIFIC_POLICY_VERSION,
        python_version=_provenance_text(
            runtime, "python_version", platform.python_version()),
        primer3_py_version=(
            primer3_py_version or _provenance_text(
                dependencies, "primer3_py_version",
                _package_version("primer3-py"))),
        primer3_core_version=(
            primer3_core_version or _provenance_text(
                dependencies, "underlying_primer3_version")),
        viennarna_version=(
            vienna_version or _provenance_text(
                dependencies, "viennarna_version",
                _package_version("ViennaRNA"))),
        vienna_parameter_set=_provenance_text(
            dependencies, "vienna_parameter_set", VIENNA_PARAMETER_SET),
        vienna_salt_applied=bool(vienna_salt_applied),
        operating_system=_provenance_text(
            runtime, "platform", f"{platform.system()} {platform.release()}"),
        architecture=_provenance_text(
            runtime, "architecture", platform.machine()),
        source_commit=_source_commit(),
        source_tree_hash=source_tree_hash(),
        dependency_lock_hash=dependency_lock_hash(),
        build_date=_provenance_text(
            provenance, "build_utc", os.environ.get("MBUPRIME_BUILD_DATE", "")),
        release_artifact_hash=release_artifact_hash(),
        max_variants=max_variants,
        enumeration_pool=enumeration_pool,
        min_sequence_length=min_sequence_length,
        max_sequence_length=max_sequence_length,
        engine_search_policies=_ordered_mapping(engine_search_policies),
        final_union_truncated=bool(final_union_truncated),
        degenerate_structure_scope=degenerate_structure_scope,
        severity_policy=_ordered_mapping(severity_policy),
        geometry_policy=_ordered_mapping(geometry_policy),
        variant_policy=_ordered_mapping(variant_policy),
        dg_decision_resolution=str(DG_DECISION_RESOLUTION),
        tm_decision_resolution=str(TM_DECISION_RESOLUTION),
        salt_formula_id=SALT_FORMULA_ID,
        normalized_inputs=tuple(
            _ordered_mapping(item) for item in normalized_inputs),
        conditions=_ordered_mapping(conditions),
        diagnostics=tuple(diagnostics),
        engines=tuple(
            (engine, _freeze_value(metadata))
            for engine, metadata in sorted((engines or {}).items())),
        ensemble_plan=_ordered_mapping(ensemble_plan or {}),
        ensemble_coverage=_ordered_mapping(ensemble_coverage or {}),
    )
    provisional = ScientificManifest(**common, analysis_id="")
    analysis_id = analysis_identity_sha256(provisional)
    return ScientificManifest(**common, analysis_id=analysis_id)
