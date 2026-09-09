"""Validated Python boundary for the bundled RNAstructure 6.6 extension.

The native module is deliberately narrow: DNA hairpin search, duplex search,
and exact fixed-CT scoring.  It never resolves executables or changes DATAPATH.
"""

from __future__ import annotations

from collections import namedtuple
import hashlib
import importlib
import json
import threading
from pathlib import Path
import sysconfig
from typing import Iterable

from native_target import UnsupportedNativeTargetError, current_target

_PACKAGE = Path(__file__).resolve().parent
_SOURCE_ARCHIVE = "vendor/rnastructure-6.6/RNAstructureSource.zip"
_SOURCE_ARCHIVE_SHA256 = (
    "4e30fa06f10a89556ad070c8d141fff6090165c330df786e19f9627df4407fd4")
_UPSTREAM_SOURCE_MANIFEST_SHA256 = (
    "481a55eed8a10d77bcf1e052a9ad6c528096101b740ff916314a92c34da65b5a")
_REQUIRED_MANIFEST_FIELDS = frozenset({
    "schema_version", "integration", "engine_version", "target",
    "abi_tag", "compiler", "native_module",
    "native_module_sha256", "upstream_source_archive_sha256",
    "upstream_source_manifest_sha256", "source_manifest_sha256",
    "dna_table_manifest_sha256", "compatibility_patch",
    "upstream_source_archive", "source_files", "upstream_source_files",
    "dna_table_files",
})
_ManifestContext = namedtuple(
    "_ManifestContext", ("target", "manifest_path", "manifest"))


class NativeIntegrityError(RuntimeError):
    """The bundled native engine or one of its scientific tables is untrusted."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _mapping_sha256(payload: dict[str, str]) -> str:
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _expected_abi_tag() -> str:
    extension_suffix = str(sysconfig.get_config_var("EXT_SUFFIX") or "")
    fallback = extension_suffix.removeprefix(".")
    for suffix in (".pyd", ".so"):
        fallback = fallback.removesuffix(suffix)
    return str(sysconfig.get_config_var("SOABI") or fallback)


def _manifest_path_for_target(target: str) -> Path:
    return _PACKAGE / "native_manifests" / f"{target}.json"


def _collect_manifest_context() -> _ManifestContext:
    try:
        target = current_target()
    except UnsupportedNativeTargetError as exc:
        raise NativeIntegrityError(str(exc)) from exc
    manifest_path = _manifest_path_for_target(target)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NativeIntegrityError(f"native manifest is unavailable: {exc}") from exc
    return _ManifestContext(target, manifest_path, manifest)


def _validate_manifest_identity(context: _ManifestContext) -> None:
    manifest = context.manifest
    missing = _REQUIRED_MANIFEST_FIELDS.difference(manifest)
    if missing:
        raise NativeIntegrityError(
            "native manifest is incomplete: " + ", ".join(sorted(missing)))
    if manifest["schema_version"] != 3:
        raise NativeIntegrityError("RNAstructure manifest schema mismatch")
    recorded_target = manifest["target"]
    if recorded_target != context.target:
        raise NativeIntegrityError(
            "RNAstructure manifest target mismatch: "
            f"{recorded_target} != {context.target}")
    expected_abi = _expected_abi_tag()
    if not expected_abi or manifest["abi_tag"] != expected_abi:
        raise NativeIntegrityError(
            f"RNAstructure manifest ABI mismatch: {manifest['abi_tag']} != {expected_abi}")
    expected_compiler = (
        "msvc" if context.target == "cp312-windows-x86_64" else "unix")
    if manifest["compiler"] != expected_compiler:
        raise NativeIntegrityError(
            "RNAstructure manifest compiler mismatch: "
            f"{manifest['compiler']} != {expected_compiler}")


def _validate_project_source_manifest(context: _ManifestContext) -> None:
    manifest = context.manifest
    source_files = manifest["source_files"]
    if (not isinstance(source_files, dict)
            or _mapping_sha256(source_files) != manifest["source_manifest_sha256"]):
        raise NativeIntegrityError("RNAstructure source manifest hash mismatch")


def _validate_upstream_source_manifest(context: _ManifestContext) -> None:
    manifest = context.manifest
    if (manifest["upstream_source_archive"] != _SOURCE_ARCHIVE
            or manifest["upstream_source_archive_sha256"]
            != _SOURCE_ARCHIVE_SHA256):
        raise NativeIntegrityError("RNAstructure upstream source archive hash mismatch")
    upstream_files = manifest["upstream_source_files"]
    if (not isinstance(upstream_files, dict)
            or _mapping_sha256(upstream_files)
            != manifest["upstream_source_manifest_sha256"]
            or manifest["upstream_source_manifest_sha256"]
            != _UPSTREAM_SOURCE_MANIFEST_SHA256):
        raise NativeIntegrityError(
            "RNAstructure compiled upstream source manifest mismatch")


def _validate_native_module(context: _ManifestContext) -> None:
    manifest = context.manifest
    module_name = str(manifest["native_module"])
    if Path(module_name).name != module_name:
        raise NativeIntegrityError("RNAstructure native module name is invalid")
    module_path = _PACKAGE / module_name
    if (not module_path.is_file()
            or _sha256(module_path) != manifest["native_module_sha256"]):
        raise NativeIntegrityError("RNAstructure native module hash mismatch")


def _validate_dna_tables(context: _ManifestContext) -> None:
    manifest = context.manifest
    table_files = manifest["dna_table_files"]
    if not isinstance(table_files, dict):
        raise NativeIntegrityError("DNA table manifest has invalid shape")
    if (_mapping_sha256(table_files)
            != manifest["dna_table_manifest_sha256"]):
        raise NativeIntegrityError("RNAstructure DNA table manifest hash mismatch")
    for relative, expected in table_files.items():
        path = _PACKAGE / str(relative).removeprefix("rnastructure_native/")
        if not path.is_file() or _sha256(path) != expected:
            raise NativeIntegrityError(
                f"RNAstructure DNA table hash mismatch: {relative}")
    expected_paths = {
        str(relative).removeprefix("rnastructure_native/")
        for relative in table_files
    }
    actual_paths = {
        path.relative_to(_PACKAGE).as_posix()
        for path in (_PACKAGE / "data_tables").rglob("*") if path.is_file()
    }
    if actual_paths != expected_paths:
        unexpected = sorted(actual_paths.difference(expected_paths))
        missing_paths = sorted(expected_paths.difference(actual_paths))
        raise NativeIntegrityError(
            "RNAstructure DNA table payload set mismatch; "
            f"unexpected={unexpected}, missing={missing_paths}")


def _load_and_validate_manifest() -> dict[str, object]:
    """Validate target metadata and bundled artifacts before native import.

    The installed Python package is the trust root; this bundled manifest is
    integrity metadata, not a publisher signature. Validation fails closed on
    schema, target, ABI, compiler, native-module hash, pinned upstream archive
    identity, declared source-map digest, or the complete DNA-table file set.
    Only the returned validated module name may cross the later import boundary.
    """

    context = _collect_manifest_context()
    _validate_manifest_identity(context)
    _validate_project_source_manifest(context)
    _validate_upstream_source_manifest(context)
    _validate_native_module(context)
    _validate_dna_tables(context)
    manifest = context.manifest
    return manifest


def _automatic_fold_window(sequence_length: int) -> int:
    if sequence_length <= 50:
        return 2
    if sequence_length <= 120:
        return 3
    if sequence_length <= 300:
        return 5
    if sequence_length <= 500:
        return 7
    if sequence_length <= 800:
        return 11
    if sequence_length <= 1200:
        return 15
    return 20


def _ct_lines(geometry) -> list[str]:
    if geometry.kind == "hairpin":
        sequence = geometry.sequence_a
        pairs = geometry.pairs
    else:
        sequence = geometry.sequence_a + "III" + (geometry.sequence_b or "")
        offset = len(geometry.sequence_a) + 3
        pairs = tuple((left, right + offset) for left, right in geometry.pairs)
    partners = [0] * len(sequence)
    for left, right in pairs:
        partners[left] = right + 1
        partners[right] = left + 1
    lines = [f"{len(sequence)} fixed canonical geometry"]
    for index, base in enumerate(sequence, 1):
        previous = index - 1 if index > 1 else 0
        following = index + 1 if index < len(sequence) else 0
        lines.append(
            f"{index} {base} {previous} {following} "
            f"{partners[index - 1]} {index}")
    return lines


class _CancellationBridge:
    """Forward a Python Event to the extension's atomic cancellation state."""

    def __init__(self, event, native_module) -> None:
        self._native = native_module
        self.token = native_module.make_cancel_token()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if event is not None:
            def watch() -> None:
                while not self._stop.is_set():
                    if event.is_set():
                        self._native.cancel(self.token)
                        return
                    # Wake immediately when the native call finishes, rather
                    # than making close() wait on the caller's cancellation event.
                    self._stop.wait(0.025)
            self._thread = threading.Thread(
                target=watch, name="RNAstructure-cancel", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.1)


class Backend:
    """Integrity-checked in-process RNAstructure service."""

    def __init__(self) -> None:
        self.identity = _load_and_validate_manifest()
        # Import native code only after its on-disk hash and complete table
        # payload have passed the manifest boundary.
        import_name = str(self.identity["native_module"]).split(".", 1)[0]
        self._native = importlib.import_module(f".{import_name}", __name__)
        self._scaled = _PACKAGE / "data_tables" / "scaled"
        self._enthalpy = _PACKAGE / "data_tables" / "enthalpy_as_dg"

    def runtime_identity(self) -> dict[str, object]:
        identity = {
            key: self.identity[key]
            for key in (
                "integration", "engine_version", "target", "abi_tag", "compiler",
                "native_module_sha256", "upstream_source_archive_sha256",
                "upstream_source_manifest_sha256", "source_manifest_sha256",
                "dna_table_manifest_sha256",
                "compatibility_patch",
            )
        }
        return identity

    def _invoke(self, function, *args, cancel_event=None, **kwargs):
        bridge = _CancellationBridge(cancel_event, self._native)
        try:
            return function(*args, cancel_token=bridge.token, **kwargs)
        except InterruptedError as exc:
            # Import lazily to avoid a package -> adapter import cycle.
            from external_engines import ExternalEngineCancelled
            raise ExternalEngineCancelled(str(exc)) from exc
        finally:
            bridge.close()

    def fold_hairpin(self, sequence: str, temperature_c: float,
                     maximum_structures: int, cancel_event=None):
        """Return ordered native search records for one DNA sequence at Celsius.

        Records contain zero-based pairs and search_dg_kcal_mol. Hairpin folding
        polls native cooperative cancellation; cancellation raises
        ExternalEngineCancelled after the native call unwinds.
        """
        normalized = sequence.upper().replace("U", "T")
        return self._invoke(
            self._native.fold_hairpin, normalized, str(self._scaled),
            float(temperature_c) + 273.15, int(maximum_structures),
            _automatic_fold_window(len(normalized)), cancel_event=cancel_event)

    def fold_duplex(self, sequence_a: str, sequence_b: str,
                    temperature_c: float, maximum_structures: int,
                    cancel_event=None):
        """Return ordered duplex records; each pair indexes A and B from zero.

        Inputs run 5-prime to 3-prime and temperature is Celsius. Upstream bimol
        has no progress hook: cancellation is checked before/after that phase,
        so an active fold can finish before ExternalEngineCancelled is raised.
        """
        return self._invoke(
            self._native.fold_duplex,
            sequence_a.upper().replace("U", "T"),
            sequence_b.upper().replace("U", "T"), str(self._scaled),
            float(temperature_c) + 273.15, int(maximum_structures),
            cancel_event=cancel_event)

    def score_fixed_geometries(self, geometries: Iterable[object],
                               temperature_c: float, cancel_event=None):
        """Score one sequence-identity batch in input order, at Celsius.

        Canonical hairpin pairs index the single strand from zero; duplex pairs
        index A and B separately from zero. Return one dict per geometry with
        dg_37_kcal_mol, dh_kcal_mol and dg_requested_kcal_mol; an empty input
        returns (). Mixed kind/sequence identities raise ValueError. Geometry
        transfer is in memory and never depends on TEMP's filename encoding.
        Cancellation is checked between scoring passes and structures, not
        inside a single CalculateFreeEnergy call, and raises
        ExternalEngineCancelled. No caller geometry is mutated.
        """
        geometries = tuple(geometries)
        if not geometries:
            return ()
        identities = {
            (item.kind, item.sequence_a, item.sequence_b) for item in geometries}
        if len(identities) != 1:
            raise ValueError(
                "RNAstructure fixed-geometry batch requires one sequence identity")
        first = geometries[0]
        is_duplex = first.kind != "hairpin"
        sequence = first.sequence_a
        offset = len(sequence) + 3 if is_duplex else 0
        if is_duplex:
            sequence += "III" + (first.sequence_b or "")
        pair_batches = tuple(
            tuple((left, right + offset) for left, right in geometry.pairs)
            for geometry in geometries)
        return self._invoke(
            self._native.score_ct, sequence, str(self._scaled),
            str(self._enthalpy), float(temperature_c) + 273.15,
            pair_batches=pair_batches,
            linker_start=len(first.sequence_a) + 1 if is_duplex else 0,
            cancel_event=cancel_event)


def create_backend() -> Backend:
    return Backend()
