"""Mandatory in-process RNAstructure and seqfold engine adapters.

The adapters return provenance-preserving, geometry-owned observations for the
flat peer union assembled with Primer3 and ViennaRNA results.
Finite exact-geometry energies and validated hairpin melting temperatures are
eligible for the declared severity policy; dimer melting temperatures remain
display-only. Integrity, native-calculation, and malformed-result failures are
typed and never leak invalid values into severity decisions.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import hashlib
import json
import math
import threading
from collections import OrderedDict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable, Mapping, Protocol

from native_target import UnsupportedNativeTargetError, current_target


ENGINE_ORDER = ("RNAstructure", "seqfold")
RNASTRUCTURE_STRUCTURE_LIMIT = 50
_EXACT_BATCH_CACHE_SIZE = 2048
_SINGLE_FLIGHT_WAIT_POLL_SECONDS = 0.05
_SEQFOLD_TM_SEARCH_RANGE_C = (0.0, 100.0)
_SEQFOLD_TM_TOLERANCE_C = 0.01
_SEQFOLD_AFFINE_RESIDUAL_KCAL_MOL = 1e-6
_SEQFOLD_REQUIRED_VERSION = "0.10.2"
_SEQFOLD_TARGET_IDENTITIES = {
    "cp312-windows-x86_64": {
        "artifact_sha256": "d8f075bd6738b6d82baa5bee61b3b45bc05ad5d37d2687d9bd40c54d175cf91b",
        "files": {
            "__init__.py": "bd918ad035fee1682c7ce61122f8ca78cee87b3f06d984ae6cebcda1a0609f95",
            "main.py": "26410b281cc712d7c4a75a7f62850e787a8087578b995ef77a8b27ad282285b9",
            "_core.pyd": "5ab89fefa4f32eb9fb737f379e4d158fa985dee0797d31d136bebda4275c4434",
        },
    },
    "cp312-linux-x86_64": {
        "artifact_sha256": "b79f983eb459d3d14b90a74e362aaf020fcfaa97f4cf99aab8792ac4df174e76",
        "files": {
            "__init__.py": "9a35530f9b86c2c7a263324d119ac5c28bd4760a73cbe11fc4cfe9727892d2e6",
            "main.py": "be9d5752eb2abde92219dc65963e756aa615627480cfde28e5fa9faa20f7c868",
            "_core.abi3.so": "7e731153f15ac8d2b8d248c9d75c4b825eaac9360e7551eda49b20ccc4c0be64",
        },
    },
    "cp312-macos-arm64": {
        "artifact_sha256": "7a8346fcff651d3b137086460e9bf274a30187c86812405328a2f94227b62802",
        "files": {
            "__init__.py": "9a35530f9b86c2c7a263324d119ac5c28bd4760a73cbe11fc4cfe9727892d2e6",
            "main.py": "be9d5752eb2abde92219dc65963e756aa615627480cfde28e5fa9faa20f7c868",
            "_core.abi3.so": "97e59b1c26c4f8c6d75cb5179d6f2f1229d1169ed85183148d02fe6b4b3f7964",
        },
    },
}
try:
    _SEQFOLD_CURRENT_TARGET = current_target()
except UnsupportedNativeTargetError:
    _SEQFOLD_CURRENT_TARGET = ""
_SEQFOLD_FILE_HASHES = dict(
    _SEQFOLD_TARGET_IDENTITIES.get(
        _SEQFOLD_CURRENT_TARGET, {"files": {}})["files"])


class CancelEvent(Protocol):
    def is_set(self) -> bool:
        """Return True when the current engine calculation should stop."""


class ExternalEngineCancelled(RuntimeError):
    """Raised when a cooperative cancellation stops an engine calculation."""


class RequiredScientificEngineError(RuntimeError):
    """A mandatory scientific backend is missing, corrupt, or unusable."""


class _NoStructureError(ValueError):
    """Backend completed normally but produced no physical structure."""


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _canonical_geometry_kind(raw_kind: str) -> str:
    kind = raw_kind.lower().replace("_", "-")
    kind = {"heterodimer": "hetero-dimer", "selfdimer": "self-dimer"}.get(
        kind, kind)
    if kind not in {"hairpin", "self-dimer", "hetero-dimer", "dimer"}:
        raise ValueError(f"unsupported geometry kind: {raw_kind}")
    return kind


def _canonical_geometry_sequences(
    kind: str, sequence_a: str, sequence_b: str | None,
) -> tuple[str, str | None]:
    a = sequence_a.upper().replace("U", "T")
    b = None if sequence_b is None else sequence_b.upper().replace("U", "T")
    if not a or (kind != "hairpin" and not b):
        raise ValueError("canonical geometry requires its input sequence(s)")
    if kind == "hairpin" and b is not None:
        raise ValueError("hairpin geometry must not have sequence_b")
    return a, b


def _canonical_geometry_pairs(
    kind: str,
    sequence_a: str,
    sequence_b: str | None,
    pairs: Iterable[tuple[int, int]],
) -> tuple[tuple[int, int], ...]:
    normalized: list[tuple[int, int]] = []
    for pair in pairs:
        if (not isinstance(pair, tuple) or len(pair) != 2
                or not all(isinstance(index, int) for index in pair)):
            raise ValueError("pairs must contain integer (a_index, b_index) tuples")
        left, right = pair
        right_limit = len(sequence_a) if kind == "hairpin" else len(sequence_b or "")
        if (left < 0 or right < 0 or left >= len(sequence_a)
                or right >= right_limit):
            raise ValueError("base-pair index lies outside its input sequence")
        if kind == "hairpin" and left != right:
            left, right = sorted((left, right))
        if kind == "hairpin" and left == right:
            raise ValueError("a hairpin base cannot pair with itself")
        normalized.append((left, right))
    ordered = tuple(sorted(set(normalized)))
    if len(ordered) != len(normalized):
        raise ValueError("duplicate base pairs are not allowed")
    return ordered


def _validate_canonical_geometry_topology(
    kind: str, pairs: tuple[tuple[int, int], ...],
) -> None:
    if kind == "hairpin":
        endpoints = tuple(index for pair in pairs for index in pair)
        if len(set(endpoints)) != len(endpoints):
            raise ValueError("one base cannot participate in multiple pairs")
        for index, (left, right) in enumerate(pairs):
            for next_left, next_right in pairs[index + 1:]:
                if left < next_left < right < next_right:
                    raise ValueError("hairpin pairs must be noncrossing")
        return
    if len({left for left, _ in pairs}) != len(pairs):
        raise ValueError("one base cannot participate in multiple pairs")
    if len({right for _, right in pairs}) != len(pairs):
        raise ValueError("one base cannot participate in multiple pairs")


@dataclass(frozen=True, eq=False)
class CanonicalGeometry:
    """A validated, zero-based base-pair identity in input orientation."""

    kind: str
    sequence_a: str
    sequence_b: str | None
    pairs: tuple[tuple[int, int], ...]

    def __deepcopy__(self, memo: dict[int, object]) -> CanonicalGeometry:
        # This frozen record contains only immutable values; rebuilding it in
        # every detached peer copy adds hot-path work without extra isolation.
        return self

    def __post_init__(self) -> None:
        kind = _canonical_geometry_kind(self.kind)
        a, b = _canonical_geometry_sequences(
            kind, self.sequence_a, self.sequence_b)
        ordered = _canonical_geometry_pairs(kind, a, b, self.pairs)
        _validate_canonical_geometry_topology(kind, ordered)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "sequence_a", a)
        object.__setattr__(self, "sequence_b", b)
        object.__setattr__(self, "pairs", ordered)

    @property
    def identity(self) -> tuple[object, ...]:
        direct = (self.kind, self.sequence_a, self.sequence_b or "", self.pairs)
        if self.kind == "hairpin":
            return direct
        swapped = (self.kind, self.sequence_b or "", self.sequence_a,
                   tuple(sorted((right, left) for left, right in self.pairs)))
        return min(direct, swapped)

    @property
    def pair_classes(self) -> tuple[str, ...]:
        """Classify pairs without rejecting geometries accepted by a backend."""

        sequence_b = self.sequence_a if self.kind == "hairpin" else self.sequence_b
        assert sequence_b is not None
        watson_crick = {("A", "T"), ("T", "A"), ("C", "G"), ("G", "C")}
        wobble = {("G", "T"), ("T", "G")}
        classes = []
        for left, right in self.pairs:
            bases = (self.sequence_a[left], sequence_b[right])
            classes.append(
                "watson_crick" if bases in watson_crick
                else "wobble" if bases in wobble
                else "mismatch")
        return tuple(classes)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, CanonicalGeometry) and self.identity == other.identity

    def __hash__(self) -> int:
        return hash(self.identity)


@dataclass(frozen=True)
class EngineObservation:
    """Immutable result at the adapter-to-orchestrator trust boundary.

    Enumerated engine/status values are rejected when unknown. Every numeric
    metric is normalized to a finite float; supplied non-numeric or non-finite
    values are discarded, change the status to ``parse_error``, and append a
    diagnostic. Downstream severity code therefore cannot mistake malformed
    engine output for a valid measurement.
    """

    engine: str
    engine_version: str
    status: str
    interaction_type: str
    search_mode: str
    geometry: CanonicalGeometry | None
    dg_kcal_mol: float | None
    tm_c: float | None
    energy_definition: str
    tm_definition: str
    structure_scope: str
    structure_text: str
    warning: str
    provenance: str
    # Engine-owned audit fields: seqfold uses selected-path validation;
    # RNAstructure supplies fixed-geometry H/S and hairpin/dimer Tm metadata.
    # Status and provenance may exist without a numeric value. Defaults
    # preserve the positional adapter contract.
    dh_kcal_mol: float | None = None
    ds_cal_mol_k: float | None = None
    tm_status: str = "not_calculated"
    tm_bracket_c: tuple[float, float] | None = None
    tm_search_range_c: tuple[float, float] | None = None
    tm_numerical_tolerance_c: float | None = None
    thermo_path_identity: str | None = None
    tm_method: str = "not_calculated"
    dg_temperature_c: float | None = None

    def __post_init__(self) -> None:
        if self.engine not in ENGINE_ORDER:
            raise ValueError(f"unsupported scientific engine: {self.engine}")
        if self.status not in {
            "ok", "no_structure", "not_installed", "parse_error",
            "engine_error", "unsupported_quantity",
        }:
            raise ValueError(f"unsupported engine status: {self.status}")
        raw_dg, raw_tm = self.dg_kcal_mol, self.tm_c
        normalized_dg, normalized_tm = _finite(raw_dg), _finite(raw_tm)
        discarded = []
        if raw_dg is not None and normalized_dg is None:
            discarded.append("dG")
        if raw_tm is not None and normalized_tm is None:
            discarded.append("Tm")
        if discarded:
            object.__setattr__(self, "status", "parse_error")
            suffix = f"nonfinite/non-numeric {' and '.join(discarded)} discarded"
            object.__setattr__(self, "warning",
                               f"{self.warning}; {suffix}".strip("; "))
        object.__setattr__(self, "dg_kcal_mol", normalized_dg)
        object.__setattr__(self, "tm_c", normalized_tm)
        for attribute in (
                "dh_kcal_mol", "ds_cal_mol_k",
                "tm_numerical_tolerance_c", "dg_temperature_c"):
            raw_value = getattr(self, attribute)
            normalized_value = _finite(raw_value)
            if raw_value is not None and normalized_value is None:
                object.__setattr__(self, "status", "parse_error")
                suffix = f"nonfinite/non-numeric {attribute} discarded"
                object.__setattr__(self, "warning",
                                   f"{self.warning}; {suffix}".strip("; "))
            object.__setattr__(self, attribute, normalized_value)

    def to_dict(self) -> dict[str, object]:
        geometry = self.geometry
        return {
            "engine": self.engine,
            "engine_version": self.engine_version,
            "status": self.status,
            "interaction_type": self.interaction_type,
            "search_mode": self.search_mode,
            "geometry": None if geometry is None else {
                "kind": geometry.kind,
                "sequence_a": geometry.sequence_a,
                "sequence_b": geometry.sequence_b,
                "pairs": [list(pair) for pair in geometry.pairs],
                "identity": repr(geometry.identity),
            },
            "dg_kcal_mol": self.dg_kcal_mol,
            "tm_c": self.tm_c,
            "energy_definition": self.energy_definition,
            "tm_definition": self.tm_definition,
            "structure_scope": self.structure_scope,
            "structure_text": self.structure_text,
            "warning": self.warning,
            "provenance": self.provenance,
            "dh_kcal_mol": self.dh_kcal_mol,
            "ds_cal_mol_k": self.ds_cal_mol_k,
            "tm_status": self.tm_status,
            "tm_bracket_c": (None if self.tm_bracket_c is None else
                             list(self.tm_bracket_c)),
            "tm_search_range_c": (
                None if self.tm_search_range_c is None else
                list(self.tm_search_range_c)),
            "tm_numerical_tolerance_c": self.tm_numerical_tolerance_c,
            "thermo_path_identity": self.thermo_path_identity,
            "tm_method": self.tm_method,
            "dg_temperature_c": self.dg_temperature_c,
        }


@dataclass(frozen=True)
class EngineBatch:
    observations: tuple[EngineObservation, ...]
    diagnostics: tuple[str, ...]
    statuses: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class EngineCapability:
    status: str
    version: str
    path: str
    hairpin: bool
    dimer: bool
    multiple_structures: bool
    structure_tm: bool


class _ExactBatchCache:
    """Bounded, single-flight cache for immutable successful engine batches."""

    def __init__(self, maxsize: int = _EXACT_BATCH_CACHE_SIZE) -> None:
        self._maxsize = maxsize
        self._values: OrderedDict[tuple[object, ...], EngineBatch] = OrderedDict()
        self._inflight: dict[tuple[object, ...], threading.Event] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _cacheable(batch: EngineBatch) -> bool:
        statuses = {status for _engine, status in batch.statuses}
        return bool(statuses) and statuses <= {"ok", "no_structure"}

    @staticmethod
    def _wait_for_inflight(
        waiter: threading.Event,
        cancel_event: CancelEvent | None,
    ) -> None:
        while not waiter.wait(_SINGLE_FLIGHT_WAIT_POLL_SECONDS):
            if cancel_event is not None and cancel_event.is_set():
                raise ExternalEngineCancelled(
                    "engine calculation cancelled")
        if cancel_event is not None and cancel_event.is_set():
            raise ExternalEngineCancelled(
                "engine calculation cancelled")

    def get_or_compute(
        self, key: tuple[object, ...], compute, *,
        cancel_event: CancelEvent | None = None,
        cacheable: Callable[[EngineBatch], bool] | None = None,
    ) -> EngineBatch:
        while True:
            with self._lock:
                cached = self._values.get(key)
                if cached is not None:
                    self._values.move_to_end(key)
                    return cached
                waiter = self._inflight.get(key)
                if waiter is None:
                    waiter = threading.Event()
                    self._inflight[key] = waiter
                    owner = True
                else:
                    owner = False
            if owner:
                break
            self._wait_for_inflight(waiter, cancel_event)
        try:
            value = compute()
            should_cache = self._cacheable if cacheable is None else cacheable
            if should_cache(value):
                with self._lock:
                    self._values[key] = value
                    self._values.move_to_end(key)
                    while len(self._values) > self._maxsize:
                        self._values.popitem(last=False)
            return value
        finally:
            with self._lock:
                event = self._inflight.pop(key, None)
                if event is not None:
                    event.set()

    def clear(self) -> None:
        with self._lock:
            self._values.clear()


_EXACT_BATCH_CACHE = _ExactBatchCache()
_SEQFOLD_BATCH_CACHE = _ExactBatchCache(maxsize=1024)


def _input_oriented_geometry_key(
    geometry: CanonicalGeometry,
) -> tuple[object, ...]:
    """Return geometry identity without collapsing a dimer strand swap."""

    return (
        geometry.kind,
        geometry.sequence_a,
        geometry.sequence_b or "",
        geometry.pairs,
    )


def _complete_fixed_geometry_batch(
    batch: EngineBatch,
    requested: tuple[CanonicalGeometry, ...],
) -> bool:
    """Admit only complete, finite, input-oriented fixed-score batches."""

    if not _ExactBatchCache._cacheable(batch):
        return False
    if len(batch.observations) != len(requested):
        return False
    for observation, geometry in zip(batch.observations, requested):
        if (
            observation.engine != "RNAstructure"
            or observation.status != "ok"
            or observation.geometry is None
            or observation.dg_kcal_mol is None
            or _input_oriented_geometry_key(observation.geometry)
            != _input_oriented_geometry_key(geometry)
        ):
            return False
    return True


class RNAstructureSession:
    """One analysis-scoped handle to the integrity-checked native backend."""

    def __init__(self, cancel_event: CancelEvent | None = None) -> None:
        self.cancel_event = cancel_event
        self.backend = None
        self.native_identity: dict[str, object] = {}
        self.runtime_status = "not_installed"
        self.runtime_diagnostic = "RNAstructure native backend is unavailable"

    def __enter__(self) -> "RNAstructureSession":
        try:
            self.backend = _load_rnastructure_native_backend()
            self.native_identity = self.backend.runtime_identity()
            if self.native_identity.get("engine_version") != "6.6":
                raise RequiredScientificEngineError(
                    "RNAstructure native backend must be version 6.6")
        except Exception as exc:
            self.runtime_status = "engine_error"
            self.runtime_diagnostic = f"RNAstructure native validation failed: {exc}"
        else:
            self.runtime_status = "available"
            self.runtime_diagnostic = ""
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.backend = None

    @property
    def fingerprint(self) -> tuple[object, ...]:
        import scientific_metadata as scientific_policy

        return (
            self.runtime_status, tuple(sorted(self.native_identity.items())),
            scientific_policy.SCIENTIFIC_POLICY_VERSION,
        )


@dataclass(frozen=True)
class SeqfoldHairpinThermo:
    """Fail-closed thermodynamics derived from one seqfold hairpin path.

    seqfold does not expose source-table dH/dS through Python.  Its unrounded
    dynamic-programming cache does expose dG(T), however, and the source model
    uses dG = dH - T*dS.  We derive the slope only while the complete selected
    path is invariant and affine, then validate the same path across a narrow
    bracket around the closed-form zero crossing.
    """

    status: str
    tm_c: float | None = None
    dh_kcal_mol: float | None = None
    ds_cal_mol_k: float | None = None
    bracket_c: tuple[float, float] | None = None
    numerical_tolerance_c: float = 0.01
    search_range_c: tuple[float, float] = (0.0, 100.0)
    sample_temperatures_c: tuple[float, ...] = ()
    max_affine_residual_kcal_mol: float | None = None
    path_identity: str | None = None
    warning: str = ""


@dataclass(frozen=True)
class RNAstructureThermo:
    """Status-rich thermodynamics for one fixed RNAstructure CT geometry."""

    status: str
    dg_kcal_mol: float | None = None
    dg_temperature_c: float | None = None
    tm_c: float | None = None
    dh_kcal_mol: float | None = None
    ds_cal_mol_k: float | None = None
    bracket_c: tuple[float, float] | None = None
    numerical_tolerance_c: float = 0.01
    search_range_c: tuple[float, float] = (0.0, 100.0)
    path_identity: str | None = None
    warning: str = ""
    compatibility_provenance: str = ""


def hairpin_geometry_from_dot_bracket(sequence: str,
                                      dot_bracket: str) -> CanonicalGeometry:
    sequence = sequence.upper().replace("U", "T")
    dot_bracket = dot_bracket.strip()
    if len(dot_bracket) != len(sequence):
        raise ValueError("dot-bracket length does not match sequence length")
    stack: list[int] = []
    pairs: list[tuple[int, int]] = []
    for index, marker in enumerate(dot_bracket):
        if marker == "(":
            stack.append(index)
        elif marker == ")":
            if not stack:
                raise ValueError("unbalanced dot-bracket structure")
            pairs.append((stack.pop(), index))
        elif marker not in ".-":
            raise ValueError(f"unsupported dot-bracket marker: {marker}")
    if stack:
        raise ValueError("unbalanced dot-bracket structure")
    return CanonicalGeometry("hairpin", sequence, None, tuple(pairs))


def _load_rnastructure_native_backend():
    """Load and integrity-check the mandatory bundled native package."""

    from rnastructure_native import create_backend
    return create_backend()


def _validated_seqfold_identity() -> dict[str, object]:
    """Validate the exact mandatory seqfold implementation before use."""

    try:
        distribution = importlib.metadata.distribution("seqfold")
    except importlib.metadata.PackageNotFoundError as exc:
        raise RequiredScientificEngineError(
            "mandatory seqfold backend unavailable") from exc
    version = distribution.version
    if version != _SEQFOLD_REQUIRED_VERSION:
        raise RequiredScientificEngineError(
            f"mandatory seqfold must be {_SEQFOLD_REQUIRED_VERSION}; found {version}")
    try:
        target = current_target()
    except UnsupportedNativeTargetError as exc:
        raise RequiredScientificEngineError(str(exc)) from exc
    expected_identity = _SEQFOLD_TARGET_IDENTITIES.get(target)
    if expected_identity is None:
        raise RequiredScientificEngineError(
            f"mandatory seqfold identity is unverified for target {target}")
    expected_files = _SEQFOLD_FILE_HASHES
    if not isinstance(expected_files, dict):
        raise RequiredScientificEngineError(
            f"mandatory seqfold identity has invalid shape for target {target}")
    package = Path(distribution.locate_file("seqfold")).resolve()
    try:
        spec = importlib.util.find_spec("seqfold")
    except (ImportError, ValueError) as exc:
        raise RequiredScientificEngineError(
            "mandatory seqfold import identity is unavailable") from exc
    if spec is None or spec.origin is None:
        raise RequiredScientificEngineError(
            "mandatory seqfold import target is unavailable")
    if Path(spec.origin).resolve().parent != package:
        raise RequiredScientificEngineError(
            "mandatory seqfold import target differs from its distribution")
    actual: dict[str, str] = {}
    python_source_digest = hashlib.sha256()
    native_core_name = ""
    for name, expected in sorted(expected_files.items()):
        path = package / name
        if not path.is_file():
            raise RequiredScientificEngineError(
                f"mandatory seqfold file is missing: {name}")
        contents = path.read_bytes()
        digest = hashlib.sha256(contents).hexdigest()
        if digest != expected:
            raise RequiredScientificEngineError(
                f"mandatory seqfold file hash mismatch: {name}")
        actual[name] = digest
        if path.suffix == ".py":
            python_source_digest.update(name.encode("utf-8"))
            python_source_digest.update(contents)
        else:
            native_core_name = name
    return {"version": version, "integration": "in_process_python",
            "target": target,
            "artifact_sha256": expected_identity["artifact_sha256"],
            "file_sha256": actual,
            "python_source_manifest_sha256": python_source_digest.hexdigest(),
             "native_core_sha256": actual[native_core_name]}


def _load_validated_seqfold():
    """Hash the physical seqfold payload before Python executes its package."""

    _validated_seqfold_identity()
    return importlib.import_module("seqfold")


def require_mandatory_engines() -> dict[str, object]:
    """Fail before analysis when RNAstructure or seqfold is unavailable."""

    try:
        backend = _load_rnastructure_native_backend()
        identity = backend.runtime_identity()
    except Exception as exc:
        raise RequiredScientificEngineError(
            f"mandatory RNAstructure native backend unavailable: {exc}") from exc
    seqfold_identity = _validated_seqfold_identity()
    return {"RNAstructure": identity, "seqfold": seqfold_identity}


def discover_external_engines(
    session: RNAstructureSession | None = None,
) -> dict[str, EngineCapability]:
    """Return deterministic availability records for mandatory adapters."""

    owned = session is None
    current = session or RNAstructureSession()
    if owned:
        current.__enter__()
    try:
        try:
            seqfold_identity = _validated_seqfold_identity()
        except RequiredScientificEngineError:
            seqfold = EngineCapability(
                "engine_error", "", "in-process", True, False, False, True)
        else:
            seqfold = EngineCapability(
                "available", str(seqfold_identity["version"]), "in-process", True, False,
                False, True)
        version = str(current.native_identity.get("engine_version", ""))
        status = current.runtime_status
        return {
            "RNAstructure": EngineCapability(
                status, version, "in-process",
                status == "available", status == "available",
                status == "available", status == "available"),
            "seqfold": seqfold,
        }
    finally:
        if owned:
            current.__exit__(None, None, None)


def _seqfold_cache_dg(seqfold_module, sequence: str,
                      temperature_c: float) -> float:
    """Read the unrounded full-sequence dG from seqfold's DP cache."""

    cache = seqfold_module.dg_cache(sequence, temp=float(temperature_c))
    try:
        raw_value = cache[0][-1]
    except (IndexError, KeyError, TypeError) as exc:
        raise ValueError("seqfold dg_cache had no full-sequence cell") from exc
    value = _finite(raw_value)
    if value is None:
        raise ValueError("seqfold dg_cache returned a nonfinite/non-numeric dG")
    if value == 1600.0:
        raise _NoStructureError(
            "seqfold returned 1600 no-fold sentinel, not a dG")
    return value


def _seqfold_selected_path(seqfold_module, sequence: str,
                           temperature_c: float
                           ) -> tuple[str, str]:
    """Return canonical geometry plus traceback identity at a temperature."""

    structures = seqfold_module.fold(sequence, temp=float(temperature_c))
    if not structures:
        raise _NoStructureError("seqfold returned no selected hairpin path")
    dot = seqfold_module.dot_bracket(sequence, structures)
    if isinstance(dot, (list, tuple)):
        dot = "".join(str(marker) for marker in dot)
    dot = str(dot)
    geometry = hairpin_geometry_from_dot_bracket(sequence, dot)
    # Geometry alone cannot distinguish distinct dangling-end tracebacks.
    # Include the ordered contribution descriptors and indices, but never the
    # rounded per-contribution energies (which legitimately change with T).
    contributions = tuple(
        (repr(getattr(item, "ij", None)), str(getattr(item, "desc", "")))
        for item in structures
    )
    path_identity = json.dumps(
        {"geometry": repr(geometry.identity), "traceback": contributions},
        sort_keys=True, separators=(",", ":"))
    return dot, path_identity


def _sample_seqfold_path(seqfold_module, sequence: str,
                         sample_temperatures: tuple[float, ...]):
    sample_dg: list[float] = []
    identities: list[str] = []
    for sample_c in sample_temperatures:
        sample_dg.append(_seqfold_cache_dg(
            seqfold_module, sequence, sample_c))
        _dot, identity = _seqfold_selected_path(
            seqfold_module, sequence, sample_c)
        identities.append(identity)
    return sample_dg, identities


def _fit_seqfold_affine_model(
        sample_temperatures: tuple[float, ...], sample_dg: list[float]
) -> tuple[float, float, float] | None:
    mean_t = sum(sample_temperatures) / len(sample_temperatures)
    mean_g = sum(sample_dg) / len(sample_dg)
    denominator = sum((value - mean_t) ** 2
                      for value in sample_temperatures)
    if denominator <= 0.0:
        return None
    slope = sum((temp - mean_t) * (dg - mean_g)
                for temp, dg in zip(sample_temperatures, sample_dg)) / denominator
    intercept = mean_g - slope * mean_t
    residual = max(abs(dg - (intercept + slope * temp))
                   for temp, dg in zip(sample_temperatures, sample_dg))
    return slope, intercept, residual


def _seqfold_crossing_status(tm_c: float, search_range) -> str:
    if tm_c < search_range[0]:
        return "below_search_range"
    if tm_c > search_range[1]:
        return "above_search_range"
    return "crossing_found"


def _validate_seqfold_crossing(
        seqfold_module, sequence: str, tm_c: float, tolerance: float,
        search_range, path_identity: str,
) -> tuple[str, tuple[float, float] | None, str]:
    lower = max(search_range[0], math.floor(tm_c / tolerance) * tolerance)
    upper = min(search_range[1], lower + tolerance)
    try:
        bracket_dg = (
            _seqfold_cache_dg(seqfold_module, sequence, lower),
            _seqfold_cache_dg(seqfold_module, sequence, upper),
        )
        bracket_identity = (
            _seqfold_selected_path(seqfold_module, sequence, lower)[1],
            _seqfold_selected_path(seqfold_module, sequence, upper)[1],
        )
    except Exception as exc:
        return ("calculation_failed", None,
                f"seqfold crossing validation failed: {type(exc).__name__}: {exc}")
    crosses_zero = min(bracket_dg) <= 1e-12 and max(bracket_dg) >= -1e-12
    if (bracket_identity[0] == path_identity
            and bracket_identity[1] == path_identity and crosses_zero):
        return "crossing_found", (lower, upper), ""
    status = ("path_changed"
              if len(set(bracket_identity + (path_identity,))) > 1
              else "no_sign_change")
    return status, None, (
        "seqfold zero crossing did not retain the calibrated selected path "
        "or a sign-changing bracket")


def _seqfold_hairpin_thermo(sequence: str, temperature_c: float,
                            seqfold_module=None) -> SeqfoldHairpinThermo:
    """Derive selected-path dH/dS and intrinsic hairpin Tm from seqfold.

    This intentionally never calls :func:`seqfold.tm`; that public function is
    an oligo/duplex Tm and is not a geometry-specific hairpin transition.
    """

    seqfold = seqfold_module or _load_validated_seqfold()

    search_range = _SEQFOLD_TM_SEARCH_RANGE_C
    tolerance = _SEQFOLD_TM_TOLERANCE_C
    requested = min(search_range[1], max(search_range[0],
                                         float(temperature_c)))
    sample_temperatures = tuple(sorted(set((0.0, 25.0, 50.0, requested))))
    # Retain at least three independent temperatures even if the request lies
    # on an anchor.  All anchors are within the declared search range.
    try:
        sample_dg, identities = _sample_seqfold_path(
            seqfold, sequence, sample_temperatures)
    except Exception as exc:
        return SeqfoldHairpinThermo(
            status="calculation_failed",
            search_range_c=search_range,
            numerical_tolerance_c=tolerance,
            sample_temperatures_c=sample_temperatures,
            warning=f"seqfold source-model sampling failed: {type(exc).__name__}: {exc}",
        )

    if len(set(identities)) != 1:
        return SeqfoldHairpinThermo(
            status="path_changed",
            search_range_c=search_range,
            numerical_tolerance_c=tolerance,
            sample_temperatures_c=sample_temperatures,
            path_identity=identities[0] if identities else None,
            warning=("seqfold selected geometry/traceback changed across "
                     "thermodynamic samples; dH/dS were not mixed"),
        )

    # Ordinary least squares is used instead of a two-point slope so that the
    # affine source-model assumption is itself testable.
    fitted = _fit_seqfold_affine_model(sample_temperatures, sample_dg)
    if fitted is None:
        return SeqfoldHairpinThermo(
            status="calculation_failed",
            search_range_c=search_range,
            numerical_tolerance_c=tolerance,
            sample_temperatures_c=sample_temperatures,
            path_identity=identities[0],
            warning="seqfold thermodynamic samples did not span temperature",
        )
    slope, intercept, residual = fitted
    if residual > _SEQFOLD_AFFINE_RESIDUAL_KCAL_MOL:
        return SeqfoldHairpinThermo(
            status="non_affine",
            search_range_c=search_range,
            numerical_tolerance_c=tolerance,
            sample_temperatures_c=sample_temperatures,
            max_affine_residual_kcal_mol=residual,
            path_identity=identities[0],
            warning=("seqfold selected-path dG(T) was not affine within "
                     f"{_SEQFOLD_AFFINE_RESIDUAL_KCAL_MOL:g} kcal/mol"),
        )

    if abs(slope) <= 1e-15:
        return SeqfoldHairpinThermo(
            status="no_sign_change",
            search_range_c=search_range,
            numerical_tolerance_c=tolerance,
            sample_temperatures_c=sample_temperatures,
            max_affine_residual_kcal_mol=residual,
            path_identity=identities[0],
            warning="seqfold selected-path dG(T) has no finite zero crossing",
        )

    tm_c = -intercept / slope
    status = _seqfold_crossing_status(tm_c, search_range)
    if status != "crossing_found":
        return SeqfoldHairpinThermo(
            status=status,
            search_range_c=search_range,
            numerical_tolerance_c=tolerance,
            sample_temperatures_c=sample_temperatures,
            max_affine_residual_kcal_mol=residual,
            path_identity=identities[0],
            warning=f"seqfold selected-path zero crossing is {status}",
        )

    validation_status, bracket, validation_warning = _validate_seqfold_crossing(
        seqfold, sequence, tm_c, tolerance, search_range, identities[0])
    if validation_status != "crossing_found":
        return SeqfoldHairpinThermo(
            status=validation_status,
            search_range_c=search_range,
            numerical_tolerance_c=tolerance,
            sample_temperatures_c=sample_temperatures,
            max_affine_residual_kcal_mol=residual,
            path_identity=identities[0],
            warning=validation_warning)

    ds_cal_mol_k = -1000.0 * slope
    dh_kcal_mol = intercept - 273.15 * slope
    return SeqfoldHairpinThermo(
        status="crossing_found",
        tm_c=tm_c,
        dh_kcal_mol=dh_kcal_mol,
        ds_cal_mol_k=ds_cal_mol_k,
        bracket_c=bracket,
        numerical_tolerance_c=tolerance,
        search_range_c=search_range,
        sample_temperatures_c=sample_temperatures,
        max_affine_residual_kcal_mol=residual,
        path_identity=identities[0],
        warning=("derived from seqfold source-model unrounded dg_cache; "
                 "eligible for exact-geometry hairpin severity when the "
                 "crossing is validated"),
    )


def _seqfold_hairpin(sequence: str, temperature_c: float) -> EngineObservation:
    seqfold = _load_validated_seqfold()

    structures = seqfold.fold(sequence, temp=temperature_c)
    dg = _finite(seqfold.dg(sequence, temp=temperature_c))
    if dg is None:
        raise ValueError("seqfold returned no finite dG")
    # seqfold uses 1600 as a finite no-fold/error sentinel.  Treating it as a
    # physical kcal/mol result makes the integration look successful while
    # filling reports with scientifically meaningless values.
    if dg == 1600.0:
        raise _NoStructureError(
            "seqfold returned 1600 no-fold sentinel, not a dG")
    dot = seqfold.dot_bracket(sequence, structures)
    if isinstance(dot, (list, tuple)):
        dot = "".join(str(marker) for marker in dot)
    geometry = hairpin_geometry_from_dot_bracket(sequence, str(dot))
    thermo = _seqfold_hairpin_thermo(sequence, temperature_c, seqfold)
    tm_warning = thermo.warning or (
        f"seqfold derived hairpin Tm status: {thermo.status}")
    return EngineObservation(
        "seqfold", importlib.metadata.version("seqfold"), "ok", "hairpin",
        "mfe", geometry, dg, thermo.tm_c,
        "seqfold DNA single-strand MFE-like fold score at requested temperature",
        ("intrinsic zero-dG crossing derived from selected-path source-model "
         "dH/dS; not seqfold's duplex/oligo tm()"),
        "one independently selected single-strand fold", str(dot),
        ("seqfold has no salt/concentration or multi-structure enumeration "
         f"interface; {tm_warning}"),
        "seqfold Python API",
        dh_kcal_mol=thermo.dh_kcal_mol,
        ds_cal_mol_k=thermo.ds_cal_mol_k,
        tm_status=thermo.status,
        tm_bracket_c=thermo.bracket_c,
        tm_search_range_c=thermo.search_range_c,
        tm_numerical_tolerance_c=thermo.numerical_tolerance_c,
        thermo_path_identity=thermo.path_identity,
        tm_method="source_dh_ds_from_unrounded_dg_cache",
    )


RNASTRUCTURE_HAIRPIN_TM_SEARCH_RANGE_C = (0.0, 100.0)
RNASTRUCTURE_DIMER_TM_SEARCH_RANGE_C = (-50.0, 100.0)
_RNASTRUCTURE_TM_TOLERANCE_C = 0.01
RNASTRUCTURE_DIMER_TM_FORMULA_ID = "primer3_style_ct_over_4_v1"
RNASTRUCTURE_DIMER_TM_CONCENTRATION_DIVISOR = 4.0
RNASTRUCTURE_DIMER_TM_GAS_CONSTANT_CAL_MOL_K = 1.9872
RNASTRUCTURE_DIMER_SALT_ENTROPY_FORMULA = (
    "0.368*(pair_count-1)*ln(Na_equiv_M)")
RNASTRUCTURE_DIMER_TM_SYMMETRY_POLICY = "fixed_ct_over_4_all_dimers"
RNASTRUCTURE_DIMER_TM_SYMMETRY_CAVEAT = (
    "This selected hybrid policy always uses C_T/4; exact Primer3 can use "
    "divisor 1 for symmetric self-complementary duplexes.")


def _validated_rnastructure_thermo_inputs(
    *, geometry: CanonicalGeometry, temperature_c: object,
    dh_kcal_mol: object, ds_cal_mol_k: object, path_identity: str,
    compatibility_provenance: str,
) -> tuple[str, float | None, float | None, float | None,
           RNAstructureThermo | None]:
    """Validate fixed-geometry H/S inputs in the established failure order."""

    identity = repr(geometry.identity)
    if path_identity != identity:
        return identity, None, None, None, RNAstructureThermo(
            "geometry_changed", path_identity=identity,
            warning="RNAstructure thermodynamic totals belong to another CT geometry",
            compatibility_provenance=compatibility_provenance)
    if dh_kcal_mol is None or ds_cal_mol_k is None:
        return identity, None, None, None, RNAstructureThermo(
            "missing_enthalpy_parameter", path_identity=identity,
            warning="RNAstructure enthalpy/entropy totals are incomplete",
            compatibility_provenance=compatibility_provenance)
    temperature = _finite(temperature_c)
    dh = _finite(dh_kcal_mol)
    ds = _finite(ds_cal_mol_k)
    if temperature is None or dh is None or ds is None:
        return identity, None, None, None, RNAstructureThermo(
            "calculation_failed", path_identity=identity,
            warning=(
                "RNAstructure produced a nonfinite or non-numeric thermodynamic value"),
            compatibility_provenance=compatibility_provenance)
    return identity, temperature, dh, ds, None


def _rnastructure_hairpin_tm_uncertainty(
    dh: float, ds: float, crossing: float, low: float, high: float,
) -> tuple[float, float]:
    """Propagate RNAstructure's 0.1-kcal/mol quantization to the Tm bracket."""

    dg_37 = dh - 310.15 * ds / 1000.0
    candidates = []
    for uncertain_h in (dh - 0.05, dh + 0.05):
        for uncertain_g in (dg_37 - 0.05, dg_37 + 0.05):
            denominator = uncertain_h - uncertain_g
            if denominator != 0.0:
                candidate = 310.15 * uncertain_h / denominator - 273.15
                if math.isfinite(candidate):
                    candidates.append(candidate)
    return (
        max(low, min(candidates, default=crossing)),
        min(high, max(candidates, default=crossing)),
    )


def _rnastructure_hairpin_tm_crossing(
    dh: float, ds: float,
) -> tuple[str, float | None, tuple[float, float] | None]:
    """Classify and bracket the intrinsic fixed-geometry zero-dG crossing."""

    low, high = RNASTRUCTURE_HAIRPIN_TM_SEARCH_RANGE_C
    dg_low = dh - (low + 273.15) * ds / 1000.0
    dg_high = dh - (high + 273.15) * ds / 1000.0
    if ds == 0.0:
        return "no_sign_change", None, None
    crossing = 1000.0 * dh / ds - 273.15
    if crossing < low:
        return "below_search_range", None, None
    if crossing > high:
        return "above_search_range", None, None
    if dg_low * dg_high > 0.0:
        return "no_sign_change", None, None
    return (
        "crossing_found", crossing,
        _rnastructure_hairpin_tm_uncertainty(dh, ds, crossing, low, high),
    )


def _rnastructure_thermo_from_dh_ds(
    *, geometry: CanonicalGeometry, temperature_c: object,
    dh_kcal_mol: object, ds_cal_mol_k: object, path_identity: str,
    compatibility_provenance: str = "",
) -> RNAstructureThermo:
    """Apply dG=dH-TdS to one explicitly identified CT geometry.

    The result is an intrinsic fixed-geometry zero of dG used directly for
    hairpins. The dimer runner retains its H/S and replaces this intermediate
    crossing with the concentration-adjusted hybrid calculation below.
    """

    identity, temperature, dh, ds, failure = (
        _validated_rnastructure_thermo_inputs(
            geometry=geometry, temperature_c=temperature_c,
            dh_kcal_mol=dh_kcal_mol, ds_cal_mol_k=ds_cal_mol_k,
            path_identity=path_identity,
            compatibility_provenance=compatibility_provenance))
    if failure is not None:
        return failure
    assert temperature is not None and dh is not None and ds is not None
    dg = dh - (temperature + 273.15) * ds / 1000.0
    if not math.isfinite(dg):
        return RNAstructureThermo(
            "calculation_failed", path_identity=identity,
            warning="RNAstructure requested-temperature dG is nonfinite",
            compatibility_provenance=compatibility_provenance)

    if ds == 0.0:
        return RNAstructureThermo(
            "no_sign_change", dg, temperature, dh_kcal_mol=dh,
            ds_cal_mol_k=ds, path_identity=identity,
            warning="RNAstructure fixed-geometry dG is temperature-invariant",
            compatibility_provenance=compatibility_provenance)

    status, tm, bracket = _rnastructure_hairpin_tm_crossing(dh, ds)
    warning = "" if status == "crossing_found" else (
        f"RNAstructure fixed-geometry dG=0 crossing status: {status}")
    return RNAstructureThermo(
        status, dg, temperature, tm, dh, ds, bracket,
        max(_RNASTRUCTURE_TM_TOLERANCE_C,
            0.5 * (bracket[1] - bracket[0]) if bracket else 0.01),
        RNASTRUCTURE_HAIRPIN_TM_SEARCH_RANGE_C,
        identity, warning, compatibility_provenance)


def _rnastructure_dimer_tm_uncertainty_bracket(
    dh: float,
    ds: float,
    salt_entropy: float,
    association_entropy: float,
    crossing: float,
    low: float,
    high: float,
) -> tuple[float, float]:
    g37 = dh - 310.15 * ds / 1000.0
    candidates: list[float] = []
    for uncertain_h in (dh - 0.05, dh + 0.05):
        for uncertain_g37 in (g37 - 0.05, g37 + 0.05):
            uncertain_ds = (
                1000.0 * (uncertain_h - uncertain_g37) / 310.15)
            uncertain_denominator = (
                uncertain_ds + salt_entropy + association_entropy)
            if abs(uncertain_denominator) <= 1e-12:
                continue
            candidate = (
                1000.0 * uncertain_h / uncertain_denominator - 273.15)
            if math.isfinite(candidate):
                candidates.append(candidate)
    return (
        max(low, min(candidates, default=crossing)),
        min(high, max(candidates, default=crossing)),
    )


def _validated_rnastructure_dimer_tm_inputs(
        *, geometry: CanonicalGeometry, dh_kcal_mol: object,
        ds_cal_mol_k: object, strand_concentration_nM: object, conditions,
        path_identity: str, compatibility_provenance: str,
):
    identity = repr(geometry.identity)
    common = {
        "path_identity": identity,
        "compatibility_provenance": compatibility_provenance,
    }
    if path_identity != identity:
        return RNAstructureThermo(
            "geometry_changed", warning=(
                "RNAstructure thermodynamic totals belong to another CT "
                "geometry"), **common)
    if geometry.kind == "hairpin":
        return RNAstructureThermo(
            "calculation_failed",
            warning="concentration-adjusted dimer Tm requires a dimer geometry",
            **common)
    concentration_nM = _finite(strand_concentration_nM)
    if concentration_nM is None or concentration_nM <= 0.0:
        return RNAstructureThermo(
            "invalid_concentration", warning=(
                "strand concentration must be finite and greater than zero"),
            **common)
    if dh_kcal_mol is None or ds_cal_mol_k is None:
        return RNAstructureThermo(
            "missing_enthalpy_parameter",
            warning="RNAstructure enthalpy/entropy totals are incomplete",
            **common)
    values = (
        _finite(dh_kcal_mol), _finite(ds_cal_mol_k),
        _finite(getattr(conditions, "mv_conc", None)),
        _finite(getattr(conditions, "dv_conc", None)),
        _finite(getattr(conditions, "dntp_conc", None)),
    )
    if None in values:
        return RNAstructureThermo(
            "calculation_failed", warning=(
                "RNAstructure dimer Tm received a nonfinite or non-numeric "
                "thermodynamic/salt value"), **common)
    dh, ds, mv, dv, dntp = values
    assert all(value is not None for value in values)
    free_mg_mM = dv - dntp
    if free_mg_mM <= 0.0:
        return RNAstructureThermo(
            "calculation_failed",
            warning="free Mg2+ (dv_conc - dntp_conc) must be greater than zero",
            **common)
    effective_na_M = (mv + 120.0 * math.sqrt(free_mg_mM)) / 1000.0
    if effective_na_M <= 0.0 or not math.isfinite(effective_na_M):
        return RNAstructureThermo(
            "calculation_failed",
            warning="effective monovalent-equivalent salt must be positive",
            **common)
    pair_count = len(geometry.pairs)
    if pair_count < 1:
        return RNAstructureThermo(
            "calculation_failed",
            warning="dimer Tm requires at least one canonical base pair",
            **common)
    return common, dh, ds, concentration_nM, effective_na_M, pair_count


def _rnastructure_dimer_tm_from_dh_ds(
    *, geometry: CanonicalGeometry, dh_kcal_mol: object,
    ds_cal_mol_k: object, strand_concentration_nM: object, conditions,
    path_identity: str, compatibility_provenance: str = "",
) -> RNAstructureThermo:
    """Calculate a concentration-adjusted Tm for one fixed dimer CT.

    RNAstructure supplies exact-geometry H/S.  This function applies the
    Primer3-style salt entropy and bimolecular C_T/4 association terms while
    preserving RNAstructure's 0.1-kcal/mol H/G37 uncertainty.
    """

    validated = _validated_rnastructure_dimer_tm_inputs(
        geometry=geometry, dh_kcal_mol=dh_kcal_mol,
        ds_cal_mol_k=ds_cal_mol_k,
        strand_concentration_nM=strand_concentration_nM,
        conditions=conditions, path_identity=path_identity,
        compatibility_provenance=compatibility_provenance)
    if isinstance(validated, RNAstructureThermo):
        return validated
    common, dh, ds, concentration_nM, effective_na_M, pair_count = validated

    salt_length_term = pair_count - 1
    concentration_M = concentration_nM * 1e-9
    salt_entropy = (
        salt_length_term * 0.368 * math.log(effective_na_M))
    association_entropy = (
        RNASTRUCTURE_DIMER_TM_GAS_CONSTANT_CAL_MOL_K
        * math.log(concentration_M
                   / RNASTRUCTURE_DIMER_TM_CONCENTRATION_DIVISOR))
    denominator = ds + salt_entropy + association_entropy
    if not math.isfinite(denominator) or abs(denominator) <= 1e-12:
        return RNAstructureThermo(
            "singular_denominator", dh_kcal_mol=dh, ds_cal_mol_k=ds,
            warning="dimer Tm entropy denominator is zero or nonfinite",
            **common)
    crossing = 1000.0 * dh / denominator - 273.15
    if not math.isfinite(crossing):
        return RNAstructureThermo(
            "calculation_failed", dh_kcal_mol=dh, ds_cal_mol_k=ds,
            warning="RNAstructure concentration-adjusted dimer Tm is nonfinite",
            **common)

    low, high = RNASTRUCTURE_DIMER_TM_SEARCH_RANGE_C
    if crossing < low:
        status, tm, bracket = "below_search_range", None, None
    elif crossing > high:
        status, tm, bracket = "above_search_range", None, None
    else:
        status, tm = "crossing_found", crossing
        bracket = _rnastructure_dimer_tm_uncertainty_bracket(
            dh, ds, salt_entropy, association_entropy, crossing, low, high,
        )
    warning = "" if status == "crossing_found" else (
        "RNAstructure concentration-adjusted dimer Tm status: " + status)
    tolerance = max(
        _RNASTRUCTURE_TM_TOLERANCE_C,
        0.5 * (bracket[1] - bracket[0]) if bracket else 0.01)
    return RNAstructureThermo(
        status, tm_c=tm, dh_kcal_mol=dh, ds_cal_mol_k=ds,
        bracket_c=bracket, numerical_tolerance_c=tolerance,
        search_range_c=(low, high), warning=warning,
        **common)


def _geometry_has_loop_over_30(geometry: CanonicalGeometry) -> bool:
    pairs = sorted(geometry.pairs)
    if not pairs:
        return False
    if geometry.kind == "hairpin":
        for (left, right), (next_left, next_right) in zip(pairs, pairs[1:]):
            if next_left - left - 1 + right - next_right - 1 > 30:
                return True
        innermost = max(pairs, key=lambda pair: pair[0])
        return innermost[1] - innermost[0] - 1 > 30
    ordered = sorted(pairs, key=lambda pair: pair[0])
    for (left, right), (next_left, next_right) in zip(ordered, ordered[1:]):
        if next_left - left - 1 + abs(next_right - right) - 1 > 30:
            return True
    return False


def _rnastructure_run(
    sequence_a: str, sequence_b: str | None, conditions,
    structure_limit: int, *, session: RNAstructureSession | None = None,
    cancel_event: CancelEvent | None = None,
) -> EngineBatch:
    """Search and exactly rescore structures through the native API only."""

    if session is None:
        with RNAstructureSession(cancel_event) as owned_session:
            return _rnastructure_run(
                sequence_a, sequence_b, conditions, structure_limit,
                session=owned_session, cancel_event=cancel_event)
    if session.runtime_status != "available" or session.backend is None:
        return EngineBatch(
            (), (session.runtime_diagnostic,),
            (("RNAstructure", session.runtime_status),))
    backend = session.backend
    temperature_c = float(conditions.dg_temp_c)
    try:
        if sequence_b is None:
            raw = backend.fold_hairpin(
                sequence_a, temperature_c, structure_limit,
                cancel_event=cancel_event)
        else:
            raw = backend.fold_duplex(
                sequence_a, sequence_b, temperature_c, structure_limit,
                cancel_event=cancel_event)
    except ExternalEngineCancelled:
        raise
    except Exception as exc:
        return EngineBatch(
            (), (f"RNAstructure native search failed: {type(exc).__name__}: {exc}",),
            (("RNAstructure", "engine_error"),))

    kind = ("hairpin" if sequence_b is None else
            "self-dimer" if sequence_a == sequence_b else "hetero-dimer")
    candidates: list[tuple[CanonicalGeometry, float | None, int]] = []
    diagnostics: list[str] = []
    for index, item in enumerate(raw):
        try:
            geometry = CanonicalGeometry(
                kind, sequence_a, sequence_b,
                tuple(tuple(pair) for pair in item["pairs"]))
        except (KeyError, TypeError, ValueError) as exc:
            diagnostics.append(
                f"RNAstructure native structure {index + 1} rejected: {exc}")
            continue
        candidates.append((
            geometry, _finite(item.get("search_dg_kcal_mol")), index))
    if not candidates:
        return EngineBatch(
            (), tuple(diagnostics or ("RNAstructure reported no structures",)),
            (("RNAstructure", "no_structure"),))
    search_metadata = {
        "algorithm": "FoldSingleStrand" if sequence_b is None else "bimol",
        "maximum_structures": int(structure_limit),
        "temperature_k": temperature_c + 273.15,
        "hairpin_percent": 10,
        "duplex_percent": 40,
        "duplex_window": 0,
        "duplex_max_internal_loop": 6,
    }
    return _rnastructure_fixed_geometry_batch(
        tuple((geometry, search_dg, raw_index,
               "mfe" if raw_index == 0 else "suboptimal")
              for geometry, search_dg, raw_index in candidates),
        conditions, session, cancel_event=cancel_event,
        diagnostics=tuple(diagnostics), search_metadata=search_metadata)


def _rnastructure_fixed_score_plan(
    candidates: tuple[
        tuple[CanonicalGeometry, float | None, int, str], ...
    ],
) -> tuple[tuple[CanonicalGeometry, ...], tuple[int, ...]]:
    """Deduplicate score work while retaining candidate projection order."""

    unique_candidates: list[CanonicalGeometry] = []
    score_indexes: list[int] = []
    index_by_geometry: dict[tuple[object, ...], int] = {}
    for geometry, _search_dg, _raw_index, _search_mode in candidates:
        key = _input_oriented_geometry_key(geometry)
        score_index = index_by_geometry.get(key)
        if score_index is None:
            score_index = len(unique_candidates)
            index_by_geometry[key] = score_index
            unique_candidates.append(geometry)
        score_indexes.append(score_index)
    return tuple(unique_candidates), tuple(score_indexes)


def _rnastructure_fixed_score_provenance(
    session: RNAstructureSession,
    temperature_c: float,
    search_metadata: dict[str, object] | None,
) -> str:
    """Serialize the stable fixed-score integration provenance."""

    return json.dumps({
        "integration": "in_process_native",
        "native_identity": session.native_identity,
        "search": search_metadata or {
            "algorithm": "policy_constrained_subgeometry",
            "temperature_k": temperature_c + 273.15,
        },
        "fixed_geometry_rescorer": {
            "algorithm": "CalculateFreeEnergy(simple=True)",
            "passes": ["G37", "enthalpy-as-G", "requested-temperature"],
        },
    }, sort_keys=True, separators=(",", ":"))


def _rnastructure_fixed_score_observation(
    candidate: tuple[CanonicalGeometry, float | None, int, str],
    score: Mapping[str, object],
    *,
    conditions,
    temperature_c: float,
    version: str,
    provenance: str,
) -> tuple[EngineObservation | None, str | None]:
    """Project one native fixed score into its exact-geometry observation."""

    geometry, search_dg, raw_index, search_mode = candidate
    dg37 = _finite(score.get("dg_37_kcal_mol"))
    dh = _finite(score.get("dh_kcal_mol"))
    requested = _finite(score.get("dg_requested_kcal_mol"))
    if dg37 is None or dh is None or requested is None:
        return None, (
            f"RNAstructure native structure {raw_index + 1} had incomplete scores")
    if _geometry_has_loop_over_30(geometry):
        thermo = RNAstructureThermo(
            "non_affine", dg_kcal_mol=requested,
            dg_temperature_c=temperature_c,
            path_identity=repr(geometry.identity),
            warning=("RNAstructure loop exceeds 30 nt; polymer prelog "
                     "scaling makes inner dH/dS/Tm unsupported"),
            compatibility_provenance=provenance)
    else:
        ds = 1000.0 * (dh - dg37) / 310.15
        thermo = _rnastructure_thermo_from_dh_ds(
            geometry=geometry, temperature_c=temperature_c,
            dh_kcal_mol=dh, ds_cal_mol_k=ds,
            path_identity=repr(geometry.identity),
            compatibility_provenance=provenance)
        thermo = replace(
            thermo, dg_kcal_mol=requested,
            warning="; ".join(part for part in (
                thermo.warning,
                "reported dG is direct requested-temperature in-process "
                "fixed-geometry score at 0.1 kcal/mol engine resolution",
                (None if search_dg is None else
                 f"native search energy={search_dg:g} kcal/mol"),
            ) if part))
    dimer = geometry.kind != "hairpin"
    if dimer:
        dimer_tm = _rnastructure_dimer_tm_from_dh_ds(
            geometry=geometry,
            dh_kcal_mol=thermo.dh_kcal_mol,
            ds_cal_mol_k=thermo.ds_cal_mol_k,
            strand_concentration_nM=conditions.primer_conc,
            conditions=conditions,
            path_identity=thermo.path_identity or repr(geometry.identity),
            compatibility_provenance=provenance)
        thermo = replace(
            dimer_tm, dg_kcal_mol=requested,
            dg_temperature_c=temperature_c,
            warning="; ".join(dict.fromkeys(
                part for part in (thermo.warning, dimer_tm.warning) if part)))
    observation = EngineObservation(
        "RNAstructure", version, "ok",
        "hairpin" if not dimer else "dimer",
        search_mode, geometry,
        thermo.dg_kcal_mol, thermo.tm_c,
        "RNAstructure in-process exact-geometry DNA dG",
        ("Primer3-style concentration-adjusted fixed-geometry dimer Tm"
         if dimer else "intrinsic fixed-geometry dG=0 crossing"),
        "exact canonical CT geometry", "",
        thermo.warning, provenance,
        dh_kcal_mol=thermo.dh_kcal_mol,
        ds_cal_mol_k=thermo.ds_cal_mol_k,
        tm_status=thermo.status,
        tm_bracket_c=thermo.bracket_c,
        tm_search_range_c=thermo.search_range_c,
        tm_numerical_tolerance_c=thermo.numerical_tolerance_c,
        thermo_path_identity=thermo.path_identity,
        tm_method=("rnastructure_primer3_style_dimer_tm" if dimer else
                   "rnastructure_fixed_geometry_inner_dh_ds"),
        dg_temperature_c=thermo.dg_temperature_c,
    )
    return observation, None


def _rnastructure_fixed_geometry_batch(
    candidates: tuple[
        tuple[CanonicalGeometry, float | None, int, str], ...
    ],
    conditions,
    session: RNAstructureSession,
    *,
    cancel_event: CancelEvent | None = None,
    diagnostics: tuple[str, ...] = (),
    search_metadata: dict[str, object] | None = None,
) -> EngineBatch:
    """Exactly score one bounded, same-sequence set of canonical CT maps."""

    if session.runtime_status != "available" or session.backend is None:
        return EngineBatch(
            (), tuple(diagnostics) + (session.runtime_diagnostic,),
            (("RNAstructure", session.runtime_status),))
    temperature_c = float(conditions.dg_temp_c)
    unique_candidates, score_indexes = _rnastructure_fixed_score_plan(
        candidates)
    try:
        scores = session.backend.score_fixed_geometries(
            unique_candidates, temperature_c,
            cancel_event=cancel_event)
    except ExternalEngineCancelled:
        raise
    except Exception as exc:
        return EngineBatch(
            (), diagnostics + (
                f"RNAstructure native fixed-geometry scoring failed: "
                f"{type(exc).__name__}: {exc}",),
            (("RNAstructure", "engine_error"),))
    if len(scores) != len(unique_candidates):
        return EngineBatch(
            (), diagnostics + (
                "RNAstructure native fixed-score count mismatch",),
            (("RNAstructure", "engine_error"),))

    native_identity = session.native_identity
    provenance = _rnastructure_fixed_score_provenance(
        session, temperature_c, search_metadata)
    observations: list[EngineObservation] = []
    version = str(native_identity.get("engine_version", "6.6"))
    mutable_diagnostics = list(diagnostics)
    for candidate_index, candidate in enumerate(candidates):
        observation, diagnostic = _rnastructure_fixed_score_observation(
            candidate, scores[score_indexes[candidate_index]],
            conditions=conditions, temperature_c=temperature_c,
            version=version, provenance=provenance)
        if diagnostic is not None:
            mutable_diagnostics.append(diagnostic)
            continue
        assert observation is not None
        observations.append(observation)
    status = "ok" if observations else "engine_error"
    return EngineBatch(tuple(observations), tuple(mutable_diagnostics),
                       (("RNAstructure", status),))


def score_rnastructure_fixed_geometries(
    geometries: Iterable[CanonicalGeometry], conditions, *,
    session: RNAstructureSession | None = None,
    cancel_event: CancelEvent | None = None,
) -> EngineBatch:
    """Score supplied canonical geometries without running a fold search.

    This is the public in-process boundary used by scientific policies that
    remove bonds from an engine-discovered parent geometry.  Returned metrics
    are owned only by the supplied geometry and are explicitly marked as
    policy-derived rather than engine-discovered.
    """

    normalized = tuple(geometries)
    if not normalized:
        return EngineBatch((), (), (("RNAstructure", "no_structure"),))
    identities = {
        (item.kind, item.sequence_a, item.sequence_b) for item in normalized}
    if len(identities) != 1:
        raise ValueError(
            "RNAstructure fixed-geometry scoring requires one sequence identity")
    if session is None:
        with RNAstructureSession(cancel_event) as owned_session:
            return score_rnastructure_fixed_geometries(
                normalized, conditions, session=owned_session,
                cancel_event=cancel_event)
    if cancel_event is not None and cancel_event.is_set():
        raise ExternalEngineCancelled("engine calculation cancelled")
    candidates = tuple(
        (geometry, None, index, "policy_derived_fixed_geometry")
        for index, geometry in enumerate(normalized))
    key = (
        "rnastructure_fixed_geometry_v1",
        tuple(_input_oriented_geometry_key(geometry)
              for geometry in normalized),
        float(conditions.dg_temp_c),
        float(conditions.mv_conc),
        float(conditions.dv_conc),
        float(conditions.dntp_conc),
        float(conditions.primer_conc),
        session.fingerprint,
        id(_rnastructure_fixed_geometry_batch),
    )
    return _EXACT_BATCH_CACHE.get_or_compute(
        key,
        lambda: _rnastructure_fixed_geometry_batch(
            candidates, conditions, session, cancel_event=cancel_event),
        cancel_event=cancel_event,
        cacheable=lambda batch: _complete_fixed_geometry_batch(
            batch, normalized),
    )


def _combine(batches: Iterable[EngineBatch]) -> EngineBatch:
    observations: list[EngineObservation] = []
    diagnostics: list[str] = []
    statuses: list[tuple[str, str]] = []
    for batch in batches:
        observations.extend(batch.observations)
        diagnostics.extend(batch.diagnostics)
        statuses.extend(batch.statuses)
    return EngineBatch(tuple(observations), tuple(diagnostics), tuple(statuses))


def _rnastructure_request_key(
    sequence_a: str, sequence_b: str | None, conditions,
    structure_limit: int, session: RNAstructureSession,
) -> tuple[object, ...]:
    return (
        "rnastructure_request_v1",
        "hairpin" if sequence_b is None else "dimer",
        sequence_a.upper().replace("U", "T"),
        None if sequence_b is None else sequence_b.upper().replace("U", "T"),
        float(conditions.dg_temp_c), float(conditions.mv_conc),
        float(conditions.dv_conc), float(conditions.dntp_conc),
        float(conditions.primer_conc), int(structure_limit),
        session.fingerprint, id(_rnastructure_run),
    )


def _cached_rnastructure_run(
    sequence_a: str, sequence_b: str | None, conditions,
    structure_limit: int, *, session: RNAstructureSession | None,
    cancel_event: CancelEvent | None,
) -> EngineBatch:
    if session is None:
        return _rnastructure_run(
            sequence_a, sequence_b, conditions, structure_limit,
            cancel_event=cancel_event)
    key = _rnastructure_request_key(
        sequence_a, sequence_b, conditions, structure_limit, session)
    return _EXACT_BATCH_CACHE.get_or_compute(
        key,
        lambda: _rnastructure_run(
            sequence_a, sequence_b, conditions, structure_limit,
            session=session, cancel_event=cancel_event),
        cancel_event=cancel_event,
    )


def clear_external_result_cache() -> None:
    """Clear exact process-local engine results (primarily for tests/tools)."""

    _EXACT_BATCH_CACHE.clear()
    _SEQFOLD_BATCH_CACHE.clear()


def run_hairpin_engines(
    sequence, conditions, *,
    session: RNAstructureSession | None = None,
    cancel_event: CancelEvent | None = None,
    rnastructure_only: bool = False,
) -> EngineBatch:
    batches = [_cached_rnastructure_run(
        sequence, None, conditions, RNASTRUCTURE_STRUCTURE_LIMIT,
        session=session, cancel_event=cancel_event)]
    if rnastructure_only:
        return batches[0]
    batches.append(run_seqfold_hairpin_engine(sequence, conditions))
    return _combine(batches)


def run_seqfold_hairpin_engine(sequence, conditions) -> EngineBatch:
    """Run the in-process seqfold opinion on the caller/coordinator thread."""

    version = str(_validated_seqfold_identity()["version"])
    import scientific_metadata as scientific_policy
    key = (
        "seqfold_hairpin_v1", sequence.upper().replace("U", "T"),
        float(conditions.dg_temp_c), version,
        scientific_policy.SCIENTIFIC_POLICY_VERSION, id(_seqfold_hairpin),
    )

    def compute() -> EngineBatch:
        return _run_seqfold_hairpin_uncached(sequence, conditions)

    return _SEQFOLD_BATCH_CACHE.get_or_compute(key, compute)


def _run_seqfold_hairpin_uncached(sequence, conditions) -> EngineBatch:
    """Uncached seqfold boundary; deterministic failures stay retryable."""

    try:
        observation = _seqfold_hairpin(sequence, float(conditions.dg_temp_c))
    except ModuleNotFoundError:
        return EngineBatch((), ("seqfold is not installed",),
                           (("seqfold", "not_installed"),))
    except _NoStructureError as exc:
        return EngineBatch((), (str(exc),), (("seqfold", "no_structure"),))
    except Exception as exc:
        return EngineBatch(
            (), (f"seqfold failed: {type(exc).__name__}: {exc}",),
            (("seqfold", "engine_error"),))
    else:
        diagnostics = (() if observation.tm_status == "crossing_found" else (
            "seqfold hairpin Tm unavailable: "
            f"{observation.tm_status}; {observation.warning}",
        ))
        return EngineBatch(
            (observation,), diagnostics, (("seqfold", "ok"),))


def run_dimer_engines(
    sequence_a, sequence_b, conditions,
    self_dimer=False, *, session: RNAstructureSession | None = None,
    cancel_event: CancelEvent | None = None,
) -> EngineBatch:
    del self_dimer  # interaction type is derived from the normalized sequences
    return _cached_rnastructure_run(
        sequence_a, sequence_b, conditions, RNASTRUCTURE_STRUCTURE_LIMIT,
        session=session, cancel_event=cancel_event)


def engine_metrics(observations: Iterable[EngineObservation]) -> dict[str, tuple[float | None, float | None]]:
    """Return one deterministic display pair per engine without cross-engine math."""

    result = {engine: (None, None) for engine in ENGINE_ORDER}
    for observation in observations:
        if observation.status != "ok":
            continue
        current_dg, current_tm = result[observation.engine]
        result[observation.engine] = (
            current_dg if current_dg is not None else observation.dg_kcal_mol,
            current_tm if current_tm is not None else observation.tm_c,
        )
    return result


def observations_json(observations: Iterable[EngineObservation]) -> str:
    return json.dumps([item.to_dict() for item in observations], sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False, allow_nan=False)
