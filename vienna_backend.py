"""
vienna_backend.py
-----------------
Mandatory second-opinion thermodynamics via ViennaRNA's loop-based model, so
structure dG can be compared against Primer3's two-state ``thal`` engine.

The package import remains guarded so the application can present an actionable
startup error, but scientific analysis requires an operational ViennaRNA build
with writable ``md.salt`` support.

Conventions used for cross-engine comparison:
  * DNA parameters: Mathews 2004 DNA set.
  * Temperature: the tool's dG temperature (default 25 C).
  * Salt: monovalent-equivalent [Na+] (Mg2+ folded in the same way as the NN
    module), passed to ViennaRNA's salt correction.
  * Hairpin  -> MFE/suboptimal discovery and fixed-geometry evaluation,
    including fixed-geometry dG=0 crossing searches.
  * Dimer    -> BINDING free energy  E(AB) - E(A) - E(B)  (isolates the
    intermolecular interaction; ViennaRNA's raw cofold MFE also includes each
    strand's self-structure and would read more negative).

All values are kcal/mol.
"""

from __future__ import annotations

import math
import os
import re
import sys
from dataclasses import dataclass
from functools import lru_cache
import threading

from nn_thermo import na_equiv_M
from scientific_metadata import (
    REQUIRED_VIENNARNA_VERSION,
    ScientificDiagnostic,
    exception_diagnostic,
    finite_or_none,
)

try:
    import RNA  # ViennaRNA
    _OK = True
except Exception:  # pragma: no cover - preserve actionable mandatory preflight
    RNA = None
    _OK = False

_PARAM_LOCK = threading.Lock()
_FIXED_FOLD_LOCK = threading.Lock()
_LOG_FILTER_LOCK = threading.Lock()
_DNA_PARAMS_LOADED = False
_VIENNA_LOG_FILTER_INSTALLED = False
_MFE_CACHE_SIZE = 4096
# Holds the 21-point scan plus one geometry's bisection working set.
_FIXED_FOLD_CACHE_SIZE = 32
_TM_MIN_C = 0.0
_TM_MAX_C = 100.0
_TM_TOLERANCE_C = 0.01
_TM_SCAN_STEPS = 20
_SUBOPT_ZUKER_NOISE = re.compile(r"Added [0-9]+ bps")
_SUBOPT_ZUKER_SOURCE = "src/ViennaRNA/subopt/subopt_zuker.c"


class ViennaBackendError(RuntimeError):
    """Raised when the mandatory ViennaRNA scientific backend is unusable."""


@dataclass(frozen=True)
class FixedStructureTmResult:
    """Status-rich result for a fixed-geometry dG=0 crossing."""

    value: float | None
    status: str
    bracket: tuple[float, float] | None
    numerical_tolerance: float = _TM_TOLERANCE_C
    search_range: tuple[float, float] = (_TM_MIN_C, _TM_MAX_C)
    diagnostic: ScientificDiagnostic | None = None


@dataclass(frozen=True)
class ViennaStructure:
    """Discovered geometry with its associated Vienna energy.

    Hairpins retain their MFE/suboptimal search energy; dimers store a
    rescored fixed-geometry binding energy."""

    search_mode: str
    pairs: tuple[tuple[int, int], ...]
    structure: str
    dg_kcal_mol: float


def available() -> bool:
    return _OK


def version() -> str:
    return RNA.__version__ if _OK else ""


def _ensure_dna_params_loaded() -> None:
    """ViennaRNA DNA parameters are process-global; load them once per process."""
    global _DNA_PARAMS_LOADED
    if _DNA_PARAMS_LOADED:
        return
    with _PARAM_LOCK:
        if not _DNA_PARAMS_LOADED:
            RNA.params_load_DNA_Mathews2004()
            _DNA_PARAMS_LOADED = True


def _is_known_subopt_zuker_noise(record, installed_version) -> bool:
    """Match only ViennaRNA 2.7.2's misclassified Zuker progress record."""
    try:
        source = record.get("file_name")
        message = record.get("message")
        return (
            installed_version == "2.7.2"
            and record.get("level") == 40
            and record.get("line_number") == 1063
            and isinstance(source, str)
            and source.replace("\\", "/").endswith(_SUBOPT_ZUKER_SOURCE)
            and isinstance(message, str)
            and _SUBOPT_ZUKER_NOISE.fullmatch(message) is not None
        )
    except Exception:
        return False


def _vienna_log_callback(record, data) -> None:
    """Drop the known false error and faithfully forward every other record."""
    try:
        if _is_known_subopt_zuker_noise(record, data):
            return
    except Exception:
        pass

    try:
        level = record.get("level", "unknown")
        level_name = {
            10: "DEBUG", 20: "INFO", 30: "WARNING",
            40: "ERROR", 50: "CRITICAL",
        }.get(level, f"LEVEL {level}")
        source = record.get("file_name", "unknown source")
        line = record.get("line_number", "unknown line")
        message = record.get("message", repr(record))
        rendered = f"[{level_name}] {source}:{line}: {message}\n"
    except Exception:
        rendered = f"[VIENNARNA] {record!r}\n"

    try:
        sys.stderr.write(rendered)
        sys.stderr.flush()
    except Exception:
        try:
            os.write(2, rendered.encode("utf-8", errors="backslashreplace"))
        except Exception:
            pass


def _ensure_vienna_log_filter() -> None:
    """Install the selective ViennaRNA log callback once per process."""
    global _VIENNA_LOG_FILTER_INSTALLED
    if not _OK or _VIENNA_LOG_FILTER_INSTALLED:
        return
    with _LOG_FILTER_LOCK:
        if _VIENNA_LOG_FILTER_INSTALLED:
            return
        RNA.log_cb_add(
            _vienna_log_callback, version(), None, RNA.LOG_LEVEL_DEBUG)
        RNA.log_options_set(RNA.log_options() | RNA.LOG_OPTION_QUIET)
        _VIENNA_LOG_FILTER_INSTALLED = True


def _condition_key(cond) -> tuple[float, float]:
    return float(cond.dg_temp_c), float(na_equiv_M(cond))


def _model_from_values(temp_c: float, salt_m: float):
    _ensure_dna_params_loaded()
    md = RNA.md()
    md.temperature = temp_c
    if not hasattr(md, "salt"):
        raise ViennaBackendError(
            "ViennaRNA build does not expose the required md.salt API")
    try:
        md.salt = salt_m                # mol/L monovalent equivalent
    except Exception as exc:
        raise ViennaBackendError(
            "ViennaRNA rejected the required salt configuration") from exc
    applied = finite_or_none(
        getattr(md, "salt", None), engine="ViennaRNA", quantity="salt",
        none_is_diagnostic=False)
    if applied is None or not math.isclose(applied, salt_m, rel_tol=1e-9,
                                           abs_tol=1e-12):
        raise ViennaBackendError(
            "ViennaRNA did not retain the requested salt concentration")
    return md


def require_operational(cond) -> str:
    """Fail fast unless the mandatory DNA parameter and salt APIs work."""

    if not _OK:
        raise ViennaBackendError(
            f"ViennaRNA {REQUIRED_VIENNARNA_VERSION} is required "
            "(import name: RNA)")
    installed = version()
    if installed != REQUIRED_VIENNARNA_VERSION:
        raise ViennaBackendError(
            f"ViennaRNA must be exactly {REQUIRED_VIENNARNA_VERSION} for "
            "reproducible DNA salt calculations; installed version is "
            f"{installed or 'unknown'}")
    try:
        _model_from_values(float(cond.dg_temp_c), float(na_equiv_M(cond)))
    except ViennaBackendError:
        raise
    except Exception as exc:
        raise ViennaBackendError(
            f"ViennaRNA operational preflight failed: {type(exc).__name__}: {exc}"
        ) from exc
    return installed


def _hairpin_dot_bracket(seq: str, display_pattern: str) -> str | None:
    """Translate one enumerated DNA hairpin into ViennaRNA dot-bracket.

    The enumerator uses ``/`` and ``\\`` for the nested 5' and 3' stem arms,
    with ``-`` for unpaired nucleotides.  A constrained score is meaningful
    only when that depiction is a complete, balanced, non-crossing hairpin on
    the supplied full sequence.  Invalid geometry is rejected rather than
    silently re-folded into a different structure.
    """
    if not seq or len(seq) != len(display_pattern):
        return None
    if any(ch not in "/\\-" for ch in display_pattern):
        return None
    first_close = display_pattern.find("\\")
    if first_close != -1 and "/" in display_pattern[first_close + 1:]:
        return None

    depth = 0
    pairs = 0
    dot_bracket: list[str] = []
    for ch in display_pattern:
        if ch == "/":
            depth += 1
            pairs += 1
            dot_bracket.append("(")
        elif ch == "\\":
            depth -= 1
            if depth < 0:
                return None
            dot_bracket.append(")")
        else:
            dot_bracket.append(".")
    if depth != 0 or pairs == 0:
        return None
    return "".join(dot_bracket)


def _pairs_from_dot_bracket(structure: str) -> tuple[tuple[int, int], ...]:
    stack: list[int] = []
    pairs: list[tuple[int, int]] = []
    for index, marker in enumerate(structure.replace("&", "")):
        if marker == "(":
            stack.append(index)
        elif marker == ")":
            if not stack:
                raise ValueError("ViennaRNA returned unbalanced dot-bracket")
            pairs.append((stack.pop(), index))
        elif marker != ".":
            raise ValueError("ViennaRNA returned unsupported structure notation")
    if stack:
        raise ValueError("ViennaRNA returned unbalanced dot-bracket")
    return tuple(sorted(pairs))


def discover_hairpin_structures(
    seq: str, cond, limit: int = 50,
    diagnostics: list[ScientificDiagnostic] | None = None,
) -> tuple[ViennaStructure, ...]:
    """Return the MFE and first deterministic unique Zuker suboptimals."""

    if not _OK:
        return ()
    try:
        md = _model_from_values(*_condition_key(cond))
        compound = RNA.fold_compound(seq, md)
        mfe_structure, mfe_energy = compound.mfe()
        raw = [("mfe", str(mfe_structure), float(mfe_energy))]
        _ensure_vienna_log_filter()
        zuker = sorted(
            ((str(item.structure), float(item.energy))
             for item in compound.subopt_zuker()),
            key=lambda item: (item[1], item[0]),
        )
        raw.extend(("zuker", structure, energy)
                   for structure, energy in zuker)
        unique: dict[tuple[tuple[int, int], ...], ViennaStructure] = {}
        for search_mode, structure, energy in raw:
            pairs = _pairs_from_dot_bracket(structure)
            if not pairs or pairs in unique:
                continue
            value = finite_or_none(
                energy, engine="ViennaRNA", quantity="discovered_hairpin_dg",
                diagnostics=diagnostics)
            if value is None:
                continue
            unique[pairs] = ViennaStructure(
                search_mode, pairs, structure, value)
            if len(unique) >= limit:
                break
        return tuple(unique.values())
    except Exception as exc:
        if diagnostics is not None:
            diagnostics.append(exception_diagnostic(
                "ViennaRNA", "hairpin_structure_search", exc))
        return ()


def discover_dimer_structures(
    sequence_a: str, sequence_b: str, cond,
    diagnostics: list[ScientificDiagnostic] | None = None,
) -> tuple[ViennaStructure, ...]:
    """Return ViennaRNA's geometry-bearing dimer MFE when it has inter-pairs."""

    if not _OK:
        return ()
    try:
        md = _model_from_values(*_condition_key(cond))
        structure, _raw_energy = RNA.fold_compound(
            sequence_a + "&" + sequence_b, md).mfe_dimer()
        all_pairs = _pairs_from_dot_bracket(str(structure))
        boundary = len(sequence_a)
        pairs = tuple((left, right - boundary) for left, right in all_pairs
                      if left < boundary <= right)
        if not pairs:
            return ()
        value = dimer_structure_dg(
            sequence_a, sequence_b, pairs, cond, diagnostics)
        if value is None:
            return ()
        return (ViennaStructure("mfe", pairs, str(structure), value),)
    except Exception as exc:
        if diagnostics is not None:
            diagnostics.append(exception_diagnostic(
                "ViennaRNA", "dimer_structure_search", exc))
        return ()


@lru_cache(maxsize=_MFE_CACHE_SIZE)
def _fold_mfe_cached(seq: str, temp_c: float, salt_m: float) -> float:
    # ViennaRNA fold compounds are expensive; candidate rescoring repeats the
    # same single-strand terms many times across peer dimer geometries.
    md = _model_from_values(temp_c, salt_m)
    _structure, mfe = RNA.fold_compound(seq, md).mfe()
    return float(mfe)


@lru_cache(maxsize=_MFE_CACHE_SIZE)
def _dimer_mfe_cached(a: str, b: str, temp_c: float, salt_m: float) -> float:
    md = _model_from_values(temp_c, salt_m)
    return float(RNA.fold_compound(a + "&" + b, md).mfe_dimer()[1])


@lru_cache(maxsize=_FIXED_FOLD_CACHE_SIZE)
def _hairpin_fold_compound_cached(seq: str, temp_c: float, salt_m: float):
    """Reuse immutable fixed-fold context across sibling hairpin geometries."""
    return RNA.fold_compound(seq, _model_from_values(temp_c, salt_m))


def _hairpin_structure_energy(seq: str, dot_bracket: str,
                              temp_c: float, salt_m: float) -> float:
    """Evaluate the supplied, fixed hairpin geometry; never re-fold it."""
    # Vienna does not promise that one native fold compound is re-entrant.
    with _FIXED_FOLD_LOCK:
        compound = _hairpin_fold_compound_cached(seq, temp_c, salt_m)
        return float(compound.eval_structure(dot_bracket))


@lru_cache(maxsize=_MFE_CACHE_SIZE)
def _hairpin_structure_energy_cached(seq: str, dot_bracket: str,
                                     temp_c: float, salt_m: float) -> float:
    return _hairpin_structure_energy(seq, dot_bracket, temp_c, salt_m)


def hairpin_dg(seq: str, cond,
               diagnostics: list[ScientificDiagnostic] | None = None
               ) -> float | None:
    if not _OK:
        if diagnostics is not None:
            diagnostics.append(ScientificDiagnostic(
                "backend_not_installed", "ViennaRNA", "hairpin_dg",
                "mandatory ViennaRNA backend is unavailable"))
        return None
    try:
        value = _fold_mfe_cached(seq, *_condition_key(cond))
        return finite_or_none(value, engine="ViennaRNA", quantity="hairpin_dg",
                              diagnostics=diagnostics)
    except Exception as exc:
        if diagnostics is not None:
            diagnostics.append(exception_diagnostic(
                "ViennaRNA", "hairpin_dg", exc))
        return None


def hairpin_structure_dg(
    seq: str, display_pattern: str, cond,
    diagnostics: list[ScientificDiagnostic] | None = None,
) -> float | None:
    """Return dG for one displayed full-length hairpin geometry.

    Unlike :func:`hairpin_dg`, this does not ask ViennaRNA to find an MFE.  It
    evaluates the exact stem/loop geometry supplied by the caller using
    the Mathews-2004 DNA parameters at the configured dG temperature and salt.
    """
    if not _OK:
        if diagnostics is not None:
            diagnostics.append(ScientificDiagnostic(
                "backend_not_installed", "ViennaRNA", "hairpin_structure_dg",
                "mandatory ViennaRNA backend is unavailable"))
        return None
    dot_bracket = _hairpin_dot_bracket(seq, display_pattern)
    if dot_bracket is None:
        if diagnostics is not None:
            diagnostics.append(ScientificDiagnostic(
                "invalid_geometry", "ViennaRNA", "hairpin_structure_dg",
                "display pattern is not a complete balanced hairpin"))
        return None
    try:
        value = _hairpin_structure_energy_cached(
            seq, dot_bracket, *_condition_key(cond))
        return finite_or_none(
            value, engine="ViennaRNA", quantity="hairpin_structure_dg",
            diagnostics=diagnostics)
    except Exception as exc:
        if diagnostics is not None:
            diagnostics.append(exception_diagnostic(
                "ViennaRNA", "hairpin_structure_dg", exc))
        return None


def _hairpin_tm_scan_features(numeric_energies: list[float]):
    exact_indices = [
        index for index, energy in enumerate(numeric_energies)
        if energy == 0.0
    ]
    transitions = [
        index for index in range(len(numeric_energies) - 1)
        if ((numeric_energies[index] < 0.0)
            != (numeric_energies[index + 1] < 0.0))
    ]
    monotonic = all(
        right >= left - 1e-9
        for left, right in zip(numeric_energies, numeric_energies[1:])
    )
    return exact_indices, transitions, monotonic


def _hairpin_tm_no_crossing_status(numeric_energies: list[float]) -> str:
    if numeric_energies[-1] < 0.0:
        return "above_search_range"
    if numeric_energies[0] > 0.0:
        return "below_search_range"
    return "no_sign_change"


def _classify_hairpin_tm_scan(
    temperatures: list[float], energies: list[float | None],
) -> FixedStructureTmResult | tuple[int, list[float]]:
    """Validate a completed scan, then classify its sole crossing."""
    if any(value is None for value in energies):
        return FixedStructureTmResult(None, "calculation_failed", None)
    numeric_energies = [float(value) for value in energies]
    exact_indices, transitions, monotonic = _hairpin_tm_scan_features(
        numeric_energies)
    # An exact sampled zero is only a valid Tm after the complete scan has
    # established one monotonic transition.  Returning the first zero early
    # would mislabel plateaus or multi-root/nonmonotonic curves.
    if len(exact_indices) > 1 or len(transitions) > 1 or not monotonic:
        return FixedStructureTmResult(
            None, "nonmonotonic_or_multiple_crossings", None)
    if exact_indices:
        exact = temperatures[exact_indices[0]]
        return FixedStructureTmResult(exact, "crossing_found", (exact, exact))
    if transitions:
        return transitions[0], numeric_energies
    return FixedStructureTmResult(
        None, _hairpin_tm_no_crossing_status(numeric_energies), None)


def _refine_hairpin_tm_crossing(
    seq: str,
    dot_bracket: str,
    salt_m: float,
    temperatures: list[float],
    numeric_energies: list[float],
    crossing_index: int,
    diagnostics: list[ScientificDiagnostic] | None,
) -> FixedStructureTmResult:
    """Refine one proven monotonic scan transition without changing its bracket."""

    low, high = temperatures[crossing_index:crossing_index + 2]
    e_low = numeric_energies[crossing_index]
    e_high = numeric_energies[crossing_index + 1]
    if e_low == 0.0:
        return FixedStructureTmResult(low, "crossing_found", (low, low))
    if e_high == 0.0:
        return FixedStructureTmResult(high, "crossing_found", (high, high))
    while high - low > _TM_TOLERANCE_C:
        mid = (low + high) / 2.0
        e_mid = finite_or_none(
            _hairpin_structure_energy_cached(
                seq, dot_bracket, mid, salt_m),
            engine="ViennaRNA", quantity="hairpin_structure_tm",
            diagnostics=diagnostics)
        if e_mid is None:
            return FixedStructureTmResult(
                None, "calculation_failed", (low, high))
        if e_mid == 0.0:
            return FixedStructureTmResult(
                mid, "crossing_found", (mid, mid))
        if (e_low < 0.0) == (e_mid < 0.0):
            low, e_low = mid, e_mid
        else:
            high = mid
    value = finite_or_none(
        (low + high) / 2.0, engine="ViennaRNA",
        quantity="hairpin_structure_tm", diagnostics=diagnostics)
    return FixedStructureTmResult(
        value, "crossing_found" if value is not None else "calculation_failed",
        (low, high) if value is not None else None)


def hairpin_structure_tm(
    seq: str, display_pattern: str, cond,
    diagnostics: list[ScientificDiagnostic] | None = None,
) -> FixedStructureTmResult:
    """Return the fixed-structure dG=0 temperature in the 0--100 C range.

    This is a geometry-specific thermodynamic crossing, not Primer3's
    concentration-dependent ``thal`` hairpin Tm.  If the fixed structure does
    not bracket dG=0 within the declared range it is intentionally reported as
    unavailable.
    """
    if not _OK:
        diagnostic = ScientificDiagnostic(
            "backend_not_installed", "ViennaRNA", "hairpin_structure_tm",
            "mandatory ViennaRNA backend is unavailable")
        if diagnostics is not None:
            diagnostics.append(diagnostic)
        return FixedStructureTmResult(None, "calculation_failed", None,
                                      diagnostic=diagnostic)
    dot_bracket = _hairpin_dot_bracket(seq, display_pattern)
    if dot_bracket is None:
        diagnostic = ScientificDiagnostic(
            "invalid_geometry", "ViennaRNA", "hairpin_structure_tm",
            "display pattern is not a complete balanced hairpin")
        if diagnostics is not None:
            diagnostics.append(diagnostic)
        return FixedStructureTmResult(None, "invalid_geometry", None,
                                      diagnostic=diagnostic)
    try:
        salt_m = float(na_equiv_M(cond))
        temperatures = [
            _TM_MIN_C + i * (_TM_MAX_C - _TM_MIN_C) / _TM_SCAN_STEPS
            for i in range(_TM_SCAN_STEPS + 1)
        ]
        energies = [
            finite_or_none(
                _hairpin_structure_energy_cached(
                    seq, dot_bracket, temperature, salt_m),
                engine="ViennaRNA", quantity="hairpin_structure_tm",
                diagnostics=diagnostics)
            for temperature in temperatures
        ]
        scan_result = _classify_hairpin_tm_scan(
            temperatures, energies)
        if isinstance(scan_result, FixedStructureTmResult):
            return scan_result
        crossing_index, numeric_energies = scan_result
        return _refine_hairpin_tm_crossing(
            seq, dot_bracket, salt_m, temperatures, numeric_energies,
            crossing_index, diagnostics)
    except Exception as exc:
        diagnostic = exception_diagnostic(
            "ViennaRNA", "hairpin_structure_tm", exc)
        if diagnostics is not None:
            diagnostics.append(diagnostic)
        return FixedStructureTmResult(None, "calculation_failed", None,
                                      diagnostic=diagnostic)


def dimer_dg(a: str, b: str, cond,
             diagnostics: list[ScientificDiagnostic] | None = None
             ) -> float | None:
    """Binding free energy E(AB) - E(A) - E(B) for two strands (a may equal b
    for a self-dimer)."""
    if not _OK:
        if diagnostics is not None:
            diagnostics.append(ScientificDiagnostic(
                "backend_not_installed", "ViennaRNA", "dimer_dg",
                "mandatory ViennaRNA backend is unavailable"))
        return None
    try:
        key = _condition_key(cond)
        e_ab = _dimer_mfe_cached(a, b, *key)
        e_a = _fold_mfe_cached(a, *key)
        e_b = _fold_mfe_cached(b, *key)
        return finite_or_none(
            e_ab - e_a - e_b, engine="ViennaRNA", quantity="dimer_dg",
            diagnostics=diagnostics)
    except Exception as exc:
        if diagnostics is not None:
            diagnostics.append(exception_diagnostic(
                "ViennaRNA", "dimer_dg", exc))
        return None


def dimer_structure_dg(
    a: str, b: str, pairs: tuple[tuple[int, int], ...], cond,
    diagnostics: list[ScientificDiagnostic] | None = None,
) -> float | None:
    """Evaluate one supplied intermolecular geometry as binding free energy."""

    if not _OK or not pairs:
        return None
    try:
        chars = ["."] * (len(a) + len(b))
        for left, right in pairs:
            if not (0 <= left < len(a) and 0 <= right < len(b)):
                raise ValueError("dimer pair lies outside supplied sequences")
            chars[left] = "("
            chars[len(a) + right] = ")"
        structure = "".join(chars)
        temp_c, salt_m = _condition_key(cond)
        md = _model_from_values(temp_c, salt_m)
        complex_energy = float(
            RNA.fold_compound(a + "&" + b, md).eval_structure(structure))
        value = complex_energy - _fold_mfe_cached(a, temp_c, salt_m) - _fold_mfe_cached(
            b, temp_c, salt_m)
        return finite_or_none(
            value, engine="ViennaRNA", quantity="dimer_structure_dg",
            diagnostics=diagnostics)
    except Exception as exc:
        if diagnostics is not None:
            diagnostics.append(exception_diagnostic(
                "ViennaRNA", "dimer_structure_dg", exc))
        return None
