"""Scientific orchestration for PCR/qPCR oligonucleotide analysis.

This module owns sequence normalization, degenerate-work planning, canonical
peer union/finalization, severity classification, deterministic ranking, and
report assembly. Primer3, ViennaRNA, RNAstructure, seqfold, and the local
candidate enumerator remain behind dedicated boundaries; engine-owned metrics
are attached only to the exact geometry that produced them.

Public results, coverage ledgers, diagnostics, and export projections are
reproducibility contracts. Missing or non-finite values remain explicit and
never become benign measurements.
"""

from __future__ import annotations

import csv
import copy
import contextvars
import io
import math
import os
import re
import threading
from collections import OrderedDict
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from decimal import Decimal
from itertools import combinations, islice, product
from typing import Callable, Iterable, Iterator, Mapping, Optional, Protocol

import primer3

import external_engines as ee
import nn_thermo as nn
import scientific_metadata as sm
import headless_geometry as sd
import vienna_backend as vb

# IUPAC nucleotide codes -> the concrete bases each represents.
_IUPAC = {
    "A": "A", "C": "C", "G": "G", "T": "T",
    "R": "AG", "Y": "CT", "S": "GC", "W": "AT", "K": "GT", "M": "AC",
    "B": "CGT", "D": "AGT", "H": "ACT", "V": "ACG", "N": "ACGT",
}

# Upper bound on the number of concrete variants a single degenerate oligo may
# expand to. Keeps worst-case hetero-dimer work (N * N pairs) responsive.
MIN_SEQUENCE_LENGTH = 5
MAX_SEQUENCE_LENGTH = 60
MAX_VARIANTS = 256
VIENNA_MAX_STRUCTURES = 50
RNASTRUCTURE_MAX_STRUCTURES = ee.RNASTRUCTURE_STRUCTURE_LIMIT
LOCAL_ENUMERATION_POOL = 60
# Compatibility name used by the local enumerator and historical tooling.
ENUMERATION_POOL = LOCAL_ENUMERATION_POOL

_ACTIVE_DIAGNOSTICS: contextvars.ContextVar[
    list[sm.ScientificDiagnostic] | None
] = contextvars.ContextVar("scientific_diagnostics", default=None)
_ACTIVE_COVERAGE_OUTCOMES: contextvars.ContextVar[
    list[dict[str, object]] | None
] = contextvars.ContextVar("ensemble_coverage_outcomes", default=None)
_ACTIVE_COVERAGE_SCOPE: contextvars.ContextVar[
    tuple[object, ...] | None
] = contextvars.ContextVar("ensemble_coverage_scope", default=None)
_ACTIVE_PRIMER3_RECORDS: contextvars.ContextVar[
    dict[tuple[object, ...], tuple] | None
] = contextvars.ContextVar("primer3_context_records", default=None)
_ACTIVE_PRIMER3_RECORD_LIMIT: contextvars.ContextVar[int] = contextvars.ContextVar(
    "primer3_context_record_limit", default=0)
_DEFER_EXTERNAL: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "defer_external_engines", default=False)


class SequenceError(ValueError):
    """Raised when an input oligo sequence is empty, contains characters that
    are not valid IUPAC nucleotide codes, or is too degenerate to evaluate."""


class AnalysisCancelled(RuntimeError):
    """Raised when a caller requests cancellation during analysis."""


class AnalysisIncompleteError(RuntimeError):
    """Raised when an incomplete progressive result is passed to export."""


class CancelEvent(Protocol):
    def is_set(self) -> bool:
        """Return True when analysis should stop."""


@dataclass(frozen=True)
class AnalysisProgress:
    completed: int
    total: int
    message: str


ProgressCallback = Callable[[AnalysisProgress], None]
PartialReportCallback = Callable[["AnalysisReport"], None]


def _diagnostics() -> list[sm.ScientificDiagnostic] | None:
    return _ACTIVE_DIAGNOSTICS.get()


def _record_coverage_outcome(
    engine: str, kind: str, status: str, count: int = 1, *,
    context_id: tuple[object, ...] | None = None,
) -> None:
    outcomes = _ACTIVE_COVERAGE_OUTCOMES.get()
    if outcomes is not None:
        outcome: dict[str, object] = {
            "engine": engine,
            "kind": kind,
            "status": status,
            "count": count,
        }
        if context_id is not None:
            outcome["context_id"] = context_id
        outcomes.append(outcome)


def _coverage_context_id(
    kind: str, variant_a: int, variant_b: int | None = None,
) -> tuple[object, ...] | None:
    """Resolve one variant against the active logical interaction scope."""

    scope = _ACTIVE_COVERAGE_SCOPE.get()
    if scope is None or not scope or scope[0] != kind:
        return None
    if kind == "hairpin":
        return (kind, scope[1], variant_a, None, None)
    if kind == "self_dimer":
        return (kind, scope[1], variant_a, scope[1], variant_a)
    if kind == "hetero_dimer" and variant_b is not None:
        return (kind, scope[1], variant_a, scope[2], variant_b)
    return None


def _with_coverage_scope(
    scope: tuple[object, ...], call: Callable[[], object],
) -> object:
    token = _ACTIVE_COVERAGE_SCOPE.set(scope)
    try:
        return call()
    finally:
        _ACTIVE_COVERAGE_SCOPE.reset(token)


def _observed_discovery(
    engine: str, kind: str, call: Callable[[], Iterable[object]],
    *, context_id: tuple[object, ...] | None = None,
) -> tuple[object, ...]:
    """Run one discovery boundary and record its real terminal outcome."""

    diagnostics = _diagnostics()
    before = len(diagnostics) if diagnostics is not None else 0
    try:
        items = tuple(call())
    except BaseException:
        _record_coverage_outcome(
            engine, kind, "calculation_failed", context_id=context_id)
        raise
    new_diagnostics = (() if diagnostics is None else diagnostics[before:])
    failed = any(
        item.engine == engine
        and item.code in {
            "engine_exception", "engine_status", "engine_diagnostic",
            "parse_error", "invalid_geometry",
        }
        for item in new_diagnostics)
    _record_coverage_outcome(
        engine, kind,
        "calculation_failed" if failed else "ok" if items else "no_structure",
        context_id=context_id)
    return items


def _batch_coverage_outcome(
    batch: ee.EngineBatch, engine: str, kind: str, *,
    context_id: tuple[object, ...] | None = None,
) -> dict[str, object]:
    statuses = [status for named, status in batch.statuses if named == engine]
    terminal = statuses[-1] if len(statuses) == 1 else "calculation_failed"
    outcome: dict[str, object] = {
        "engine": engine,
        "kind": kind,
        "status": (terminal if terminal in {"ok", "no_structure"}
                   else "calculation_failed"),
        "count": 1,
    }
    if context_id is not None:
        outcome["context_id"] = context_id
    return outcome


def finite_or_none(value: object, *, engine: str = "engine",
                   quantity: str = "value") -> float | None:
    """Public common normalizer for every scientific-engine number."""

    return sm.finite_or_none(
        value, engine=engine, quantity=quantity, diagnostics=_diagnostics())


# --------------------------------------------------------------------------- #
# Reaction conditions
# --------------------------------------------------------------------------- #
@dataclass
class ReactionConditions:
    """Reaction conditions and warning thresholds.

    Defaults reflect a typical hydrolysis-probe (TaqMan) qPCR master mix.
    Concentrations follow Primer3 conventions: cations / dNTP in mM, strand
    (oligo) concentration in nM. dG values are compared in kcal/mol.
    """

    # --- buffer chemistry -------------------------------------------------- #
    mv_conc: float = 50.0     # monovalent cations Na+ / K+  (mM)
    dv_conc: float = 3.0      # divalent cations  Mg2+       (mM)
    dntp_conc: float = 0.8    # total dNTPs                  (mM)

    # --- strand concentrations -------------------------------------------- #
    primer_conc: float = 300.0   # forward / reverse primer  (nM)
    probe_conc: float = 150.0    # hydrolysis probe          (nM)

    # --- simulation temperature for structure dG -------------------------- #
    # 25 C by default for structure-dG evaluation.
    dg_temp_c: float = 25.0   # temperature at which dimer / hairpin dG is scored

    # --- universal warning thresholds (kcal/mol; more negative = more stable).
    # Applied identically to hairpins, self-dimers and hetero-dimers. -------- #
    dg_caution: float = -7.0          # any structure flagged "caution" below this
    dg_problem: float = -10.0         # any structure flagged "problem" below this

    # --- structure visibility policy ------------------------------------- #
    near_duplicate_bond_difference: int = 4
    dimer_max_consecutive_gaps: int = 0
    dimer_max_total_gaps: int = 0

    def __post_init__(self):
        self._require_finite_positive("mv_conc")
        self._require_finite_positive("dv_conc")
        self._require_finite_nonnegative("dntp_conc")
        self._require_finite_positive("primer_conc")
        self._require_finite_positive("probe_conc")
        self._require_finite("dg_temp_c")
        if self.dg_temp_c <= -273.15:
            raise SequenceError("dg_temp_c must be above absolute zero")
        self._require_finite("dg_caution")
        self._require_finite("dg_problem")
        self._require_nonnegative_integer("near_duplicate_bond_difference")
        self._require_nonnegative_integer("dimer_max_consecutive_gaps")
        self._require_nonnegative_integer("dimer_max_total_gaps")
        if self.dg_problem >= self.dg_caution:
            raise SequenceError(
                "dg_problem must be < dg_caution "
                "(more negative dG is more stable)"
            )
        if (sm.quantize_decimal(self.dg_problem, sm.DG_DECISION_RESOLUTION)
                >= sm.quantize_decimal(
                    self.dg_caution, sm.DG_DECISION_RESOLUTION)):
            raise SequenceError(
                "dG thresholds collapse at the declared 0.1 kcal/mol "
                "decision resolution")
        if self.dv_conc - self.dntp_conc <= 0:
            raise SequenceError(
                "free Mg2+ (dv_conc - dntp_conc) must be > 0")

    def _require_finite(self, name: str) -> None:
        value = getattr(self, name)
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise SequenceError(f"{name} must be a finite number")

    def _require_finite_nonnegative(self, name: str) -> None:
        self._require_finite(name)
        if getattr(self, name) < 0:
            raise SequenceError(f"{name} must be >= 0")

    def _require_finite_positive(self, name: str) -> None:
        self._require_finite(name)
        if getattr(self, name) <= 0:
            raise SequenceError(f"{name} must be > 0")

    def _require_nonnegative_integer(self, name: str) -> None:
        value = getattr(self, name)
        if isinstance(value, bool) or not isinstance(value, int):
            raise SequenceError(f"{name} must be an integer >= 0")
        if value < 0:
            raise SequenceError(f"{name} must be an integer >= 0")

    def for_conc(self, dna_conc: float) -> "ReactionConditions":
        """Return a copy with primer concentration substituted."""
        return replace(self, primer_conc=dna_conc)


# --------------------------------------------------------------------------- #
# Data holders
# --------------------------------------------------------------------------- #
@dataclass
class Oligo:
    name: str
    seq: str                 # cleaned input, may contain IUPAC codes
    role: str                # "primer" or "probe"
    conc_nM: float
    variants: list[str] = field(default_factory=list)   # concrete A/C/G/T seqs

    def __post_init__(self):
        validate_sequence_length(self.seq)
        if not self.variants:
            self.variants = expand_iupac(self.seq)

    @property
    def length(self) -> int:
        return len(self.seq)

    @property
    def n_variants(self) -> int:
        return len(self.variants)

    @property
    def is_degenerate(self) -> bool:
        return self.n_variants > 1

    @staticmethod
    def _gc(seq: str) -> float:
        return 100.0 * sum(1 for b in seq if b in "GC") / len(seq) if seq else 0.0

    @property
    def gc_min(self) -> float:
        return min(self._gc(v) for v in self.variants)

    @property
    def gc_max(self) -> float:
        return max(self._gc(v) for v in self.variants)

    def gc_display(self) -> str:
        if self.is_degenerate and abs(self.gc_max - self.gc_min) > 0.05:
            return f"{self.gc_min:.1f}-{self.gc_max:.1f}"
        return f"{self.gc_min:.1f}"


ENSEMBLE_MODE_REPRESENTATIVE = "representative"
ENSEMBLE_MODE_BUDGETED_COMPLETE = "budgeted_complete"
DEFAULT_ENSEMBLE_ADDITIONAL_BUDGET = 500
_ENSEMBLE_KINDS = ("hairpin", "self_dimer", "hetero_dimer")
_ENSEMBLE_ENGINE_SUPPORT = {
    "Primer3": frozenset(_ENSEMBLE_KINDS),
    "ViennaRNA": frozenset(_ENSEMBLE_KINDS),
    "RNAstructure": frozenset(_ENSEMBLE_KINDS),
    "seqfold": frozenset(("hairpin",)),
    "LocalEnumerator": frozenset(_ENSEMBLE_KINDS),
}


@dataclass(frozen=True)
class EnsembleContext:
    """One allocated concrete-sequence interaction context.

    The planner stores only the representative baseline plus the bounded
    additional schedule.  It never materializes an unallocated Cartesian
    variant product.
    """

    kind: str
    oligo_a: int
    variant_a: int
    sequence_a: str
    oligo_b: int | None = None
    variant_b: int | None = None
    sequence_b: str | None = None
    baseline: bool = False

    @property
    def identity(self) -> tuple[object, ...]:
        """Stable identity of one logical concrete interaction context."""

        return (
            self.kind, self.oligo_a, self.variant_a,
            self.oligo_b, self.variant_b,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "oligo_a": self.oligo_a,
            "variant_a": self.variant_a,
            "sequence_a": self.sequence_a,
            "oligo_b": self.oligo_b,
            "variant_b": self.variant_b,
            "sequence_b": self.sequence_b,
            "baseline": self.baseline,
        }


@dataclass(frozen=True)
class EnsembleKindPlan:
    baseline: int
    additional: int
    allocated: int
    total: int

    def to_dict(self) -> dict[str, int]:
        return {
            "baseline": self.baseline,
            "additional": self.additional,
            "allocated": self.allocated,
            "total": self.total,
        }


@dataclass(frozen=True)
class EnsembleWorkPlan:
    mode: str
    configured_additional_budget: int
    baseline_contexts: int
    additional_contexts: int
    allocated_contexts: int
    total_contexts: int
    by_kind: tuple[tuple[str, EnsembleKindPlan], ...]
    contexts: tuple[EnsembleContext, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "configured_additional_budget": (
                self.configured_additional_budget),
            "baseline_contexts": self.baseline_contexts,
            "additional_contexts": self.additional_contexts,
            "allocated_contexts": self.allocated_contexts,
            "total_contexts": self.total_contexts,
            "by_kind": {
                kind: values.to_dict() for kind, values in self.by_kind},
            "contexts": [item.to_dict() for item in self.contexts],
        }

    def kind(self, name: str) -> EnsembleKindPlan:
        return dict(self.by_kind)[name]


@dataclass(frozen=True)
class EnsembleCoverageEntry:
    supported: bool
    evaluated: int | None
    total: int | None
    failed: int | None

    def to_dict(self) -> dict[str, object]:
        return {
            "supported": self.supported,
            "evaluated": self.evaluated,
            "total": self.total,
            "failed": self.failed,
        }


@dataclass(frozen=True)
class EnsembleCoverage:
    mode: str
    ensemble_complete: bool
    evaluated_contexts: int
    allocated_contexts: int
    total_contexts: int
    engines: tuple[
        tuple[str, tuple[tuple[str, EnsembleCoverageEntry], ...]], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "ensemble_complete": self.ensemble_complete,
            "evaluated_contexts": self.evaluated_contexts,
            "allocated_contexts": self.allocated_contexts,
            "total_contexts": self.total_contexts,
            "engines": {
                engine: {
                    kind: entry.to_dict() for kind, entry in kinds}
                for engine, kinds in self.engines
            },
        }


def _variant_count(oligo: object) -> int:
    raw = getattr(oligo, "n_variants", None)
    if raw is None:
        raw = len(getattr(oligo, "variants"))
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise SequenceError("every oligo must contain at least one variant")
    return raw


def _context_at(
    oligos: list[object], kind: str, oligo_a: int, variant_a: int,
    oligo_b: int | None = None, variant_b: int | None = None, *,
    baseline: bool,
) -> EnsembleContext:
    variants_a = getattr(oligos[oligo_a], "variants")
    sequence_a = str(variants_a[variant_a])
    sequence_b = None
    if oligo_b is not None:
        if variant_b is None:
            raise AssertionError("dimer context requires variant_b")
        sequence_b = str(getattr(oligos[oligo_b], "variants")[variant_b])
    return EnsembleContext(
        kind, oligo_a, variant_a, sequence_a,
        oligo_b, variant_b, sequence_b, baseline)


def _take_hairpin_or_self_contexts(
    oligos: list[object], kind: str, limit: int,
) -> Iterator[EnsembleContext]:
    emitted = 0
    for oligo_index, oligo in enumerate(oligos):
        for variant_index in range(1, _variant_count(oligo)):
            if emitted >= limit:
                return
            yield _context_at(
                oligos, kind, oligo_index, variant_index, baseline=False)
            emitted += 1


def _take_heterodimer_contexts(
    oligos: list[object], limit: int,
) -> Iterator[EnsembleContext]:
    emitted = 0
    for left in range(len(oligos)):
        left_count = _variant_count(oligos[left])
        for right in range(left + 1, len(oligos)):
            right_count = _variant_count(oligos[right])
            for pair_index in range(1, left_count * right_count):
                if emitted >= limit:
                    return
                variant_a, variant_b = divmod(pair_index, right_count)
                yield _context_at(
                    oligos, "hetero_dimer", left, variant_a,
                    right, variant_b, baseline=False)
                emitted += 1


def iter_planned_ensemble_contexts(
    oligos: list[object], allocated_extra: Mapping[str, int],
) -> Iterator[EnsembleContext]:
    """Yield the arithmetic planner's deterministic context schedule."""

    oligo_count = len(oligos)
    for index in range(oligo_count):
        yield _context_at(oligos, "hairpin", index, 0, baseline=True)
    for index in range(oligo_count):
        yield _context_at(
            oligos, "self_dimer", index, 0, index, 0, baseline=True)
    for left in range(oligo_count):
        for right in range(left + 1, oligo_count):
            yield _context_at(
                oligos, "hetero_dimer", left, 0, right, 0, baseline=True)
    yield from _take_hairpin_or_self_contexts(
        oligos, "hairpin", allocated_extra["hairpin"])
    yield from _take_hairpin_or_self_contexts(
        oligos, "self_dimer", allocated_extra["self_dimer"])
    yield from _take_heterodimer_contexts(
        oligos, allocated_extra["hetero_dimer"])


def plan_ensemble_work(
    oligos: list[object], *, mode: str = ENSEMBLE_MODE_REPRESENTATIVE,
    additional_budget: int = DEFAULT_ENSEMBLE_ADDITIONAL_BUDGET,
) -> EnsembleWorkPlan:
    """Return an arithmetic-first, deterministic bounded work schedule."""

    if mode not in {
            ENSEMBLE_MODE_REPRESENTATIVE,
            ENSEMBLE_MODE_BUDGETED_COMPLETE}:
        raise SequenceError(
            "ensemble mode must be 'representative' or 'budgeted_complete'")
    if (isinstance(additional_budget, bool)
            or not isinstance(additional_budget, int)
            or additional_budget < 0):
        raise SequenceError("ensemble additional budget must be an integer >= 0")
    counts = [_variant_count(oligo) for oligo in oligos]
    oligo_count = len(oligos)
    baseline_by_kind = {
        "hairpin": oligo_count,
        "self_dimer": oligo_count,
        "hetero_dimer": oligo_count * (oligo_count - 1) // 2,
    }
    total_by_kind = {
        "hairpin": sum(counts),
        "self_dimer": sum(counts),
        "hetero_dimer": sum(
            counts[left] * counts[right]
            for left in range(oligo_count)
            for right in range(left + 1, oligo_count)),
    }
    additional_by_kind = {
        kind: total_by_kind[kind] - baseline_by_kind[kind]
        for kind in _ENSEMBLE_KINDS
    }
    remaining = (additional_budget
                 if mode == ENSEMBLE_MODE_BUDGETED_COMPLETE else 0)
    allocated_extra: dict[str, int] = {}
    for kind in _ENSEMBLE_KINDS:
        allocated_extra[kind] = min(additional_by_kind[kind], remaining)
        remaining -= allocated_extra[kind]

    contexts = list(iter_planned_ensemble_contexts(oligos, allocated_extra))

    by_kind = tuple((kind, EnsembleKindPlan(
        baseline_by_kind[kind], additional_by_kind[kind],
        baseline_by_kind[kind] + allocated_extra[kind],
        total_by_kind[kind])) for kind in _ENSEMBLE_KINDS)
    baseline_contexts = sum(baseline_by_kind.values())
    total_contexts = sum(total_by_kind.values())
    return EnsembleWorkPlan(
        mode=mode,
        configured_additional_budget=additional_budget,
        baseline_contexts=baseline_contexts,
        additional_contexts=total_contexts - baseline_contexts,
        allocated_contexts=len(contexts),
        total_contexts=total_contexts,
        by_kind=by_kind,
        contexts=tuple(contexts),
    )


def _normalize_ensemble_coverage_outcome(
    raw: Mapping[str, object],
) -> tuple[str, str, bool, int, tuple[object, ...] | None]:
    """Validate and normalize one terminal engine-coverage outcome."""

    engine = str(raw.get("engine", ""))
    kind = str(raw.get("kind", ""))
    status = str(raw.get("status", ""))
    count = raw.get("count", 1)
    if engine not in _ENSEMBLE_ENGINE_SUPPORT or kind not in _ENSEMBLE_KINDS:
        raise SequenceError("unknown ensemble coverage engine or kind")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise SequenceError("ensemble coverage count must be an integer >= 0")
    succeeded = status in {"ok", "no_structure"}
    failed = status in {
        "calculation_failed", "engine_error", "parse_error",
        "not_installed", "cancelled",
    }
    if not succeeded and not failed:
        raise SequenceError(f"unknown ensemble coverage status: {status}")
    raw_context_id = raw.get("context_id")
    if raw_context_id is None:
        return engine, kind, succeeded, count, None
    if count != 1:
        raise SequenceError(
            "identified ensemble coverage outcomes must have count=1")
    if not isinstance(raw_context_id, (tuple, list)):
        raise SequenceError("ensemble coverage context_id must be a sequence")
    context_id = tuple(raw_context_id)
    if len(context_id) != 5 or context_id[0] != kind:
        raise SequenceError(
            "ensemble coverage context_id does not match its kind")
    return engine, kind, succeeded, count, context_id


def _aggregate_ensemble_coverage_outcomes(
    outcomes: Iterable[Mapping[str, object]],
) -> tuple[
    dict[tuple[str, str], dict[str, int]],
    dict[tuple[str, str, tuple[object, ...]], bool],
]:
    """Separate anonymous ledgers from identified concrete outcomes."""

    anonymous_counts: dict[tuple[str, str], dict[str, int]] = {}
    context_statuses: dict[
        tuple[str, str, tuple[object, ...]], bool
    ] = {}
    for raw in outcomes:
        engine, kind, succeeded, count, context_id = (
            _normalize_ensemble_coverage_outcome(raw))
        if context_id is None:
            bucket = anonymous_counts.setdefault(
                (engine, kind), {"evaluated": 0, "failed": 0})
            bucket["evaluated" if succeeded else "failed"] += count
            continue
        outcome_key = (engine, kind, context_id)
        # A conflicting duplicate is a failed evaluation, never an inferred
        # success.  Normal runs emit exactly one terminal result per boundary.
        context_statuses[outcome_key] = (
            context_statuses.get(outcome_key, True) and succeeded)
    return anonymous_counts, context_statuses


def _ensemble_engine_entries(
    plan: EnsembleWorkPlan,
    anonymous_counts: Mapping[tuple[str, str], Mapping[str, int]],
    context_statuses: Mapping[
        tuple[str, str, tuple[object, ...]], bool
    ],
) -> tuple[
    tuple[str, tuple[tuple[str, EnsembleCoverageEntry], ...]], ...
]:
    """Build the stable per-engine coverage ledger."""

    plan_by_kind = dict(plan.by_kind)
    engines: list[
        tuple[str, tuple[tuple[str, EnsembleCoverageEntry], ...]]] = []
    for engine, supported_kinds in _ENSEMBLE_ENGINE_SUPPORT.items():
        entries: list[tuple[str, EnsembleCoverageEntry]] = []
        for kind in _ENSEMBLE_KINDS:
            supported = kind in supported_kinds
            if not supported:
                entry = EnsembleCoverageEntry(False, None, None, None)
            else:
                bucket = anonymous_counts.get((engine, kind), {
                    "evaluated": 0, "failed": 0})
                identified = [
                    succeeded
                    for (named_engine, named_kind, _context_id), succeeded
                    in context_statuses.items()
                    if named_engine == engine and named_kind == kind
                ]
                total = plan_by_kind[kind].total
                entry = EnsembleCoverageEntry(
                    True,
                    bucket["evaluated"] + sum(identified),
                    total,
                    bucket["failed"] + sum(not item for item in identified))
            entries.append((kind, entry))
        engines.append((engine, tuple(entries)))
    return tuple(engines)


def _completed_ensemble_context_count(
    plan: EnsembleWorkPlan,
    context_statuses: Mapping[
        tuple[str, str, tuple[object, ...]], bool
    ],
) -> int:
    """Count contexts completed by every applicable analysis engine."""
    evaluated_contexts = 0
    for context in plan.contexts:
        if all(
            context_statuses.get((engine, context.kind, context.identity)) is True
            for engine, supported_kinds in _ENSEMBLE_ENGINE_SUPPORT.items()
            if context.kind in supported_kinds
        ):
            evaluated_contexts += 1
    return evaluated_contexts


def build_ensemble_coverage(
    plan: EnsembleWorkPlan,
    outcomes: Iterable[Mapping[str, object]],
) -> EnsembleCoverage:
    """Aggregate coverage independently of structure visibility.

    Identified outcomes prove the all-applicable-engine intersection used by
    ``evaluated_contexts``. Legacy anonymous counts remain valid per-engine
    ledger entries, but cannot prove that any one concrete context completed.
    """

    anonymous_counts, context_statuses = _aggregate_ensemble_coverage_outcomes(
        outcomes)
    engines = _ensemble_engine_entries(
        plan, anonymous_counts, context_statuses)
    evaluated_contexts = _completed_ensemble_context_count(
        plan, context_statuses)
    complete = evaluated_contexts == plan.total_contexts
    return EnsembleCoverage(
        mode=plan.mode,
        ensemble_complete=complete,
        evaluated_contexts=evaluated_contexts,
        allocated_contexts=plan.allocated_contexts,
        total_contexts=plan.total_contexts,
        engines=engines,
    )


@dataclass
class TmResult:
    oligo: Oligo
    tm_mean: float
    tm_min: float
    tm_max: float
    tm_owczarzy_mean: Optional[float] = None
    tm_owczarzy_min: Optional[float] = None
    tm_owczarzy_max: Optional[float] = None

    @property
    def n_variants(self) -> int:
        return self.oligo.n_variants

    def _display_range(self, mean: float, min_value: float,
                       max_value: float) -> str:
        if self.oligo.is_degenerate and abs(max_value - min_value) > 0.05:
            return f"{min_value:.2f}-{max_value:.2f}"
        return f"{mean:.2f}"

    def tm_display(self) -> str:
        return self._display_range(self.tm_mean, self.tm_min, self.tm_max)

    def tm_owczarzy_display(self) -> str:
        if self.tm_owczarzy_mean is None:
            return ""
        return self._display_range(
            self.tm_owczarzy_mean,
            self.tm_owczarzy_min if self.tm_owczarzy_min is not None
            else self.tm_owczarzy_mean,
            self.tm_owczarzy_max if self.tm_owczarzy_max is not None
            else self.tm_owczarzy_mean,
        )


@dataclass
class PeerStructure:
    """One exact geometry in a flat, cross-engine interaction result."""
    kind: str                 # "Hairpin" | "Self-dimer" | "Hetero-dimer"
    label: str
    found: bool
    geometry: ee.CanonicalGeometry | None
    structure: str            # ASCII depiction ("" if none)
    rank: int = 0
    discovered_by: tuple[str, ...] = field(default_factory=tuple)
    dg_p3: Optional[float] = None
    tm_p3_c: Optional[float] = None
    dg_vienna: Optional[float] = None
    tm_vienna_c: Optional[float] = None
    severity: str = "unclassified"
    assessment: str = ""      # human-readable verdict
    n_combos: int = 1         # variant combinations evaluated
    representative_variant: str = ""
    involves_3prime: bool = False
    engine_observations: tuple[ee.EngineObservation, ...] = field(
        default_factory=tuple)
    external_diagnostics: tuple[str, ...] = field(default_factory=tuple)
    external_metric_states: dict[str, dict[str, str]] = field(
        default_factory=dict)
    thermo_model: str = ""
    pair_classes: tuple[str, ...] = field(default_factory=tuple)
    geometry_diagnostics: tuple[str, ...] = field(default_factory=tuple)
    derivation_lineage: tuple[dict[str, object], ...] = field(
        default_factory=tuple)

    @property
    def dg_kcal(self) -> Optional[float]:
        return self.dg_p3

    @property
    def tm_c(self) -> Optional[float]:
        return self.tm_p3_c

    @property
    def worst_variant(self) -> str:
        return self.representative_variant

    @property
    def canonical_geometry(self) -> ee.CanonicalGeometry | None:
        return self.geometry

    @property
    def metrics_by_engine(self) -> dict[str, dict[str, Optional[float]]]:
        metrics: dict[str, dict[str, Optional[float]]] = {
            "Primer3": {"dg_kcal_mol": self.dg_p3, "tm_c": self.tm_p3_c},
            "ViennaRNA": {
                "dg_kcal_mol": self.dg_vienna, "tm_c": self.tm_vienna_c},
        }
        for engine, (dg, tm) in ee.engine_metrics(self.engine_observations).items():
            metrics[engine] = {"dg_kcal_mol": dg, "tm_c": tm}
        return metrics

    @property
    def severity_drivers(self) -> tuple[str, ...]:
        dgs, tms = _finite_peer_metrics(
            self, hairpin=self.kind == "Hairpin")
        drivers: list[str] = []
        if dgs:
            drivers.append(min(dgs)[1])
        if tms:
            tm_engine = max(tms)[1]
            if tm_engine not in drivers:
                drivers.append(tm_engine)
        return tuple(drivers)

    @property
    def diagnostics(self) -> tuple[str, ...]:
        return (*self.geometry_diagnostics, *self.external_diagnostics)


@dataclass
class InteractionResult:
    """All deduplicated peer geometries for one oligo interaction."""

    kind: str
    label: str
    sequence_a: str
    sequence_b: str | None
    structures: list[PeerStructure] = field(default_factory=list)
    n_combos: int = 1
    representative_variant: str = ""
    variant_coverage: str = "representative_variant_only"
    external_diagnostics: tuple[str, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------- #
# Validation / expansion
# --------------------------------------------------------------------------- #
def clean_sequence(raw: str) -> str:
    """Remove whitespace, uppercase, and convert U to T.

    Raise SequenceError for empty input, non-IUPAC characters (including
    numbering), or length outside MIN_SEQUENCE_LENGTH..MAX_SEQUENCE_LENGTH."""
    if raw is None:
        raise SequenceError("empty sequence")
    seq = "".join(raw.split()).upper().replace("U", "T")
    if not seq:
        raise SequenceError("empty sequence")
    bad = sorted({b for b in seq if b not in _IUPAC})
    if bad:
        raise SequenceError(
            f"contains invalid base(s) {', '.join(bad)} "
            f"(allowed: A C G T and IUPAC codes R Y S W K M B D H V N)"
        )
    validate_sequence_length(seq)
    return seq


def validate_sequence_length(seq: str) -> None:
    length = len(seq)
    if not MIN_SEQUENCE_LENGTH <= length <= MAX_SEQUENCE_LENGTH:
        raise SequenceError(
            f"sequence length must be {MIN_SEQUENCE_LENGTH}-{MAX_SEQUENCE_LENGTH} "
            f"bases (received {length})")


def degeneracy_count(seq: str) -> int:
    """Number of concrete A/C/G/T sequences a (possibly degenerate) seq
    represents."""
    n = 1
    for b in seq:
        n *= len(_IUPAC[b])
    return n


def expand_iupac(seq: str, cap: int = MAX_VARIANTS) -> list[str]:
    """Expand a cleaned IUPAC sequence into all concrete variants.

    Raises SequenceError if the number of variants would exceed *cap*."""
    total = degeneracy_count(seq)
    if total > cap:
        raise SequenceError(
            f"too degenerate: {total} variants exceeds the limit of {cap}. "
            f"Reduce the number of degenerate positions or split the design."
        )
    return ["".join(p) for p in product(*(_IUPAC[b] for b in seq))]


# --------------------------------------------------------------------------- #
# Core calculations
# --------------------------------------------------------------------------- #
def _cal_to_kcal(dg_cal: float) -> float:
    return dg_cal / 1000.0


def _normalise_required_external(value: object, engine: str,
                                 quantity: str) -> float:
    number = finite_or_none(value, engine=engine, quantity=quantity)
    if number is None:
        raise RuntimeError(f"{engine} returned no finite {quantity}")
    return number


def _calc_tm_values(oligo: Oligo, cond: ReactionConditions,
                    salt_method: str) -> list[float]:
    values: list[float] = []
    for variant in oligo.variants:
        quantity = f"oligo_tm_{salt_method}"
        try:
            raw = primer3.calc_tm(
                variant,
                mv_conc=cond.mv_conc, dv_conc=cond.dv_conc,
                dntp_conc=cond.dntp_conc, dna_conc=oligo.conc_nM,
                tm_method="santalucia", salt_corrections_method=salt_method,
            )
        except Exception as exc:
            diagnostics = _diagnostics()
            if diagnostics is not None:
                diagnostics.append(sm.exception_diagnostic(
                    "Primer3", quantity, exc))
            raise RuntimeError(
                f"Primer3 failed to calculate {quantity}: "
                f"{type(exc).__name__}: {exc}") from exc
        values.append(_normalise_required_external(raw, "Primer3", quantity))
    return values


def calc_tm(oligo: Oligo, cond: ReactionConditions) -> TmResult:
    tms = _calc_tm_values(oligo, cond, "santalucia")
    tms_owc = _calc_tm_values(oligo, cond, "owczarzy")
    return TmResult(oligo=oligo, tm_mean=sum(tms) / len(tms),
                    tm_min=min(tms), tm_max=max(tms),
                    tm_owczarzy_mean=sum(tms_owc) / len(tms_owc),
                    tm_owczarzy_min=min(tms_owc),
                    tm_owczarzy_max=max(tms_owc))


def _level(dg: float, cond: ReactionConditions) -> tuple[str, str]:
    """Universal severity from a (negative) dG, same thresholds for every
    structure type."""
    decision_dg = sm.quantize_decimal(dg, sm.DG_DECISION_RESOLUTION)
    problem = sm.quantize_decimal(cond.dg_problem, sm.DG_DECISION_RESOLUTION)
    caution = sm.quantize_decimal(cond.dg_caution, sm.DG_DECISION_RESOLUTION)
    if decision_dg <= problem:
        return "problem", "Very stable"
    if decision_dg <= caution:
        return "caution", "Moderately stable"
    return "ok", "Weak"


_SEVERITY_RANK = {"ok": 0, "caution": 1, "problem": 2}
_SEVERITY_LABEL = {
    "ok": "Weak",
    "caution": "Moderately stable",
    "problem": "Very stable",
}


def _max_severity(*values: str) -> str:
    return max(values, key=lambda value: _SEVERITY_RANK[value])


def _tail_for_severity(severity: str) -> str:
    if severity == "problem":
        return "likely to interfere with PCR"
    if severity == "caution":
        return "monitor"
    return "acceptable"


def _verdict(dg_p3: Optional[float], dg_vienna: Optional[float],
             involves_3prime: bool, cond: ReactionConditions
             ) -> tuple[str, str, Optional[float], Optional[str]]:
    """Universal verdict for ANY structure (hairpin / self- / hetero-dimer,
    peer geometry).

    The warning is driven by the WORSE (more negative / more stable) of the
    available Primer3 and ViennaRNA dG values. Returns
    (severity, assessment, eff_dg, engine). eff_dg is None when no engine found
    a structure."""
    candidates = []
    p3 = sm.finite_or_none(
        dg_p3, engine="Primer3", quantity="structure_dg",
        diagnostics=_diagnostics(), none_is_diagnostic=False)
    vienna = sm.finite_or_none(
        dg_vienna, engine="ViennaRNA", quantity="structure_dg",
        diagnostics=_diagnostics(), none_is_diagnostic=False)
    if p3 is not None:
        candidates.append((p3, "Primer3"))
    if vienna is not None:
        candidates.append((vienna, "ViennaRNA"))
    if not candidates:
        return "ok", "No stable structure detected", None, None
    eff, engine = min(candidates, key=lambda c: c[0])
    sev, level = _level(eff, cond)
    site = "3' end" if involves_3prime else "internal"
    if sev == "ok":
        msg = f"{level} (min dG {eff:.2f} kcal/mol, {engine}) - acceptable"
    else:
        tail = ("likely to interfere with PCR" if sev == "problem"
                else "monitor")
        msg = (f"{level} (min dG {eff:.2f} kcal/mol, {engine}; {site}) "
               f"- {tail}")
    return sev, msg, eff, engine


def _hairpin_tm_severity(tm_c: Optional[float], pair_count: int) -> str:
    if pair_count >= 7 and (
            tm_c is None or not isinstance(tm_c, (int, float))
            or not math.isfinite(tm_c)):
        return "caution"
    if tm_c is None or not math.isfinite(tm_c):
        return "ok"
    decision_tm = sm.quantize_decimal(tm_c, sm.TM_DECISION_RESOLUTION)
    problem_threshold = {3: 65.0, 4: 60.0, 5: 55.0}.get(pair_count, 50.0)
    caution_threshold = {3: 55.0, 4: 50.0, 5: 45.0, 6: 40.0}.get(pair_count)
    if pair_count < 3:
        return "ok"
    problem = sm.quantize_decimal(
        problem_threshold, sm.TM_DECISION_RESOLUTION)
    caution = (sm.quantize_decimal(caution_threshold, sm.TM_DECISION_RESOLUTION)
               if caution_threshold is not None else None)
    if decision_tm > problem:
        return "problem"
    if pair_count >= 7 or (
        caution is not None and decision_tm > caution
    ):
        return "caution"
    return "ok"


def _tagged_rows(structure: str) -> list[tuple[str, str]]:
    tagged = [ln.split("\t", 1) for ln in structure.splitlines() if "\t" in ln]
    return [(tag, content) for tag, content in tagged]


def _hairpin_pattern(structure: str) -> str:
    tagged = _tagged_rows(structure)
    seq_rows = [c for t, c in tagged if t == "SEQ"]
    return seq_rows[0].rstrip() if seq_rows else ""


def _dimer_identity_from_rows(rows: list[tuple[str, str]]) -> Optional[tuple]:
    """Return a normalized full-sequence dimer identity from Primer3 ASCII."""
    seq_rows = [c for t, c in rows if t == "SEQ"]
    str_rows = [c for t, c in rows if t == "STR"]
    if not seq_rows or not str_rows:
        return None
    width = max(len(c) for c in seq_rows + str_rows)
    seq_rows = [c.ljust(width) for c in seq_rows]
    str_rows = [c.ljust(width) for c in str_rows]
    inner_top, inner_bot = seq_rows[-1], str_rows[0]

    def _base(tracks, col):
        return next((r[col] for r in tracks if r[col] not in " -"), None)

    top, bot = [], []
    top_ix, bot_ix = {}, {}
    for col in range(width):
        base = _base(seq_rows, col)
        if base:
            top_ix[col] = len(top)
            top.append(base)
        base = _base(str_rows, col)
        if base:
            bot_ix[col] = len(bot)
            bot.append(base)

    pairs = tuple(
        (top_ix[col], bot_ix[col])
        for col in range(width)
        if inner_top[col] not in " -" and inner_bot[col] not in " -"
        and col in top_ix and col in bot_ix
    )
    offset = pairs[0][0] - pairs[0][1] if pairs else 0
    return ("duplex", "".join(top), "".join(bot), offset, pairs)


def _dimer_identity_from_structure(structure: str) -> Optional[tuple]:
    return _dimer_identity_from_rows(_tagged_rows(structure))


def canonical_geometry_for_structure(s) -> ee.CanonicalGeometry | None:
    """Return the engine-neutral pair identity for a local result geometry."""

    try:
        if isinstance(s, PeerStructure):
            return s.geometry
        kind = getattr(s, "kind", "")
        if getattr(s, "mode", "") == "hairpin":
            opens = [index for index, marker in enumerate(s.hp_pattern)
                     if marker == "/"]
            closes = [index for index, marker in enumerate(s.hp_pattern)
                      if marker == "\\"]
            return ee.CanonicalGeometry(
                "hairpin", s.hp_seq, None, tuple(zip(opens, reversed(closes))))
        if getattr(s, "mode", "") == "duplex":
            geo_kind = "self-dimer" if kind == "Self-dimer" else "hetero-dimer"
            return ee.CanonicalGeometry(
                geo_kind, s.du_a, s.du_b, tuple(s.du_pairs))
        if kind == "Hairpin":
            sequence = (getattr(s, "representative_variant", "")
                        or getattr(s, "worst_variant", ""))
            if not sequence:
                return None
            pattern = _hairpin_pattern(getattr(s, "structure", ""))
            opens = [index for index, marker in enumerate(pattern) if marker == "/"]
            closes = [index for index, marker in enumerate(pattern) if marker == "\\"]
            return ee.CanonicalGeometry(
                "hairpin", sequence, None, tuple(zip(opens, reversed(closes))))
        identity = _dimer_identity_from_structure(getattr(s, "structure", ""))
        if identity is None:
            return None
        _tag, sequence_a, sequence_b, _offset, pairs = identity
        geo_kind = "self-dimer" if kind == "Self-dimer" else "hetero-dimer"
        input_b = sequence_b[::-1]
        input_pairs = tuple(
            (left, len(sequence_b) - 1 - right) for left, right in pairs)
        return ee.CanonicalGeometry(
            geo_kind, sequence_a, input_b, input_pairs)
    except (AttributeError, TypeError, ValueError):
        return None


def _peer_from_observation(
    observation: ee.EngineObservation, kind: str, label: str,
    n_combos: int, representative_variant: str,
) -> PeerStructure | None:
    geometry = observation.geometry
    if geometry is None or not geometry.pairs:
        return None
    return PeerStructure(
        kind=kind, label=label, found=True, geometry=geometry,
        structure=observation.structure_text,
        discovered_by=(observation.engine,),
        n_combos=n_combos, representative_variant=representative_variant,
        involves_3prime=_geometry_involves_3prime(geometry),
        engine_observations=(observation,),
        thermo_model=f"{observation.engine} {observation.search_mode}",
        pair_classes=geometry.pair_classes,
        geometry_diagnostics=_pair_class_diagnostics(geometry),
    )


_EXTERNAL_METRIC_SUPPORT = {
    "hairpin": {
        "RNAstructure": {"dg": True, "tm": True},
        "seqfold": {"dg": True, "tm": True},
    },
    "dimer": {
        "RNAstructure": {"dg": True, "tm": True},
        "seqfold": {"dg": False, "tm": False},
    },
}


def _external_engine_statuses(
    statuses: Iterable[object],
) -> dict[str, str]:
    """Normalize current and legacy engine-status representations."""

    normalized: dict[str, str] = {}
    for item in statuses:
        if isinstance(item, tuple) and len(item) == 2:
            normalized[str(item[0])] = str(item[1])
        elif isinstance(item, str):
            normalized[item] = "ok"
    return normalized


def _external_metric_state(
    *, supported: bool, attribute: str, own_observations: tuple,
    all_observations: tuple, engine_status: str | None,
) -> str:
    """Resolve one exact-geometry metric availability state."""

    if not supported:
        return "unsupported"
    if any(getattr(item, attribute) is not None for item in own_observations):
        return "available"
    if any(getattr(item, attribute) is not None for item in all_observations):
        return "different_geometry"
    if engine_status == "not_installed":
        return "not_installed"
    if engine_status in {"engine_error", "parse_error"}:
        return "calculation_failed"
    if engine_status in {"no_structure", "ok"}:
        return "no_structure" if not own_observations else "not_reported"
    return "not_run"


def _external_metric_states(structure, batch: ee.EngineBatch,
                            interaction_type: str) -> dict[str, dict[str, str]]:
    """Explain every built-in-engine metric without moving numeric results.

    ``different_geometry`` is intentionally a state rather than a copied
    number: an engine value remains owned by the exact canonical geometry
    that produced it.
    """

    supported = _EXTERNAL_METRIC_SUPPORT[interaction_type]
    # Older fixture producers used a tuple of engine names; tolerate it while
    # preserving the richer (engine, status) production contract.
    statuses = _external_engine_statuses(batch.statuses)
    own = tuple(getattr(structure, "engine_observations", ()))
    states: dict[str, dict[str, str]] = {}
    for engine in ee.ENGINE_ORDER:
        engine_own = tuple(item for item in own if item.engine == engine)
        engine_all = tuple(item for item in batch.observations
                           if item.engine == engine and item.status == "ok")
        engine_states: dict[str, str] = {}
        for quantity, attribute in (("dg", "dg_kcal_mol"), ("tm", "tm_c")):
            engine_states[quantity] = _external_metric_state(
                supported=supported[engine][quantity], attribute=attribute,
                own_observations=engine_own, all_observations=engine_all,
                engine_status=statuses.get(engine))
        states[engine] = engine_states
    return states


def _merge_external_observations(
    result: InteractionResult,
    batch: ee.EngineBatch,
    cond: ReactionConditions,
    *,
    rnastructure_session: ee.RNAstructureSession | None = None,
    cancel_event: CancelEvent | None = None,
) -> InteractionResult:
    """Add exact engine observations and rebuild the complete peer union."""

    result.external_diagnostics = tuple(batch.diagnostics)
    active = _diagnostics()
    if active is not None:
        status_engines = [engine for engine, status in batch.statuses
                          if status not in {"ok", "no_structure"}]
        for diagnostic in batch.diagnostics:
            named = next((engine for engine in ee.ENGINE_ORDER
                          if engine.lower() in diagnostic.lower()), None)
            diagnostic_engine = (named or
                                 (status_engines[0]
                                  if len(status_engines) == 1 else
                                  "structure_engines"))
            active.append(sm.ScientificDiagnostic(
                "engine_diagnostic", diagnostic_engine,
                result.kind, diagnostic))
        for engine, status in batch.statuses:
            if status not in {"ok", "no_structure"}:
                active.append(sm.ScientificDiagnostic(
                    "engine_status", engine, result.kind,
                    f"mandatory engine call status: {status}"))
    candidates = list(result.structures)
    for observation in batch.observations:
        if observation.status != "ok":
            result.external_diagnostics += (
                f"{observation.engine} result omitted: {observation.warning}",)
            continue
        peer = _peer_from_observation(
            observation, result.kind, result.label, result.n_combos,
            result.representative_variant)
        if peer is not None:
            candidates.append(peer)
    result.structures = _finalize_peer_structures(
        result.kind, result.label, result.sequence_a, result.sequence_b,
        candidates, cond, batch=batch, score_fixed_metrics=True,
        rnastructure_session=rnastructure_session,
        cancel_event=cancel_event)
    return result


def _safe_external_batch(call: Callable[[], ee.EngineBatch]) -> ee.EngineBatch:
    try:
        batch = call()
    except ee.ExternalEngineCancelled as exc:
        raise AnalysisCancelled(str(exc)) from exc
    except Exception as exc:
        raise ee.RequiredScientificEngineError(
            f"mandatory structure engine failed: {type(exc).__name__}: {exc}") from exc
    failed = [
        f"{engine}={status}" for engine, status in batch.statuses
        if engine in {"RNAstructure", "seqfold"}
        and status not in {"ok", "no_structure"}
    ]
    if failed:
        detail = "; ".join(batch.diagnostics) or ", ".join(failed)
        raise ee.RequiredScientificEngineError(
            "mandatory structure engine failed: " + detail)
    return batch


_DISCOVERY_ORDER = {
    "Primer3": 0, "ViennaRNA": 1, "RNAstructure": 2,
    "seqfold": 3, "LocalEnumerator": 4,
}


def _pair_class_diagnostics(
    geometry: ee.CanonicalGeometry,
) -> tuple[str, ...]:
    counts = {
        name: geometry.pair_classes.count(name)
        for name in ("watson_crick", "wobble", "mismatch")
        if name in geometry.pair_classes
    }
    return tuple(
        f"pair_class:{name}={count}" for name, count in counts.items())


def _geometry_involves_3prime(geometry: ee.CanonicalGeometry) -> bool:
    """Apply the declared site policy to canonical geometry only."""

    if geometry.kind == "hairpin":
        paired = {index for pair in geometry.pairs for index in pair}
        return len(geometry.sequence_a) - 1 in paired
    a_indices = {left for left, _right in geometry.pairs}
    b_indices = {right for _left, right in geometry.pairs}
    a_terminal = {len(geometry.sequence_a) - 1,
                  len(geometry.sequence_a) - 2}
    b_length = len(geometry.sequence_b or "")
    b_terminal = {b_length - 1, b_length - 2}
    return bool(a_indices & a_terminal or b_indices & b_terminal)


def _geometry_hairpin_pattern(geometry: ee.CanonicalGeometry) -> str:
    markers = ["-"] * len(geometry.sequence_a)
    for left, right in geometry.pairs:
        markers[left], markers[right] = "/", "\\"
    return "".join(markers)


def _peer_from_local(
    sub: nn.SubStructure, label: str, n_combos: int,
    representative_variant: str,
) -> PeerStructure | None:
    geometry = canonical_geometry_for_structure(sub)
    if geometry is None or not geometry.pairs:
        return None
    return PeerStructure(
        kind=sub.kind, label=label, found=True, geometry=geometry,
        structure=sub.ascii_text, discovered_by=("LocalEnumerator",),
        n_combos=n_combos, representative_variant=representative_variant,
        involves_3prime=sub.involves_3prime,
        thermo_model="LocalEnumerator geometry",
        pair_classes=geometry.pair_classes,
        geometry_diagnostics=_pair_class_diagnostics(geometry),
    )


def _peer_from_vienna(
    item: vb.ViennaStructure, kind: str, label: str, sequence_a: str,
    sequence_b: str | None, n_combos: int, representative_variant: str,
) -> PeerStructure | None:
    geometry_kind = "hairpin" if kind == "Hairpin" else (
        "self-dimer" if kind == "Self-dimer" else "hetero-dimer")
    try:
        geometry = ee.CanonicalGeometry(
            geometry_kind, sequence_a, sequence_b, item.pairs)
    except ValueError as exc:
        diagnostics = _diagnostics()
        if diagnostics is not None:
            diagnostics.append(sm.ScientificDiagnostic(
                "invalid_geometry", "ViennaRNA", kind, str(exc)))
        return None
    if kind == "Hairpin":
        pattern = _geometry_hairpin_pattern(geometry)
        structure = f"SEQ\t{pattern}\nSEQ\t{sequence_a}"
    else:
        structure = item.structure
    return PeerStructure(
        kind=kind, label=label, found=True, geometry=geometry,
        structure=structure, discovered_by=("ViennaRNA",),
        dg_vienna=item.dg_kcal_mol,
        n_combos=n_combos, representative_variant=representative_variant,
        involves_3prime=_geometry_involves_3prime(geometry),
        thermo_model=f"ViennaRNA {item.search_mode}",
        pair_classes=geometry.pair_classes,
        geometry_diagnostics=_pair_class_diagnostics(geometry),
    )


def _merge_peer_observations(target: PeerStructure, incoming: PeerStructure) -> None:
    """Retain distinct engine observations and their metric-conflict diagnostics."""
    observations = list(target.engine_observations)
    for observation in incoming.engine_observations:
        if any(item.engine == observation.engine
               and (item.dg_kcal_mol, item.tm_c)
               != (observation.dg_kcal_mol, observation.tm_c)
               for item in observations):
            target.geometry_diagnostics += (
                f"metric conflict for {observation.engine}; retained deterministic first value",
            )
    seen = {ee.observations_json((item,)) for item in observations}
    for observation in incoming.engine_observations:
        identity = ee.observations_json((observation,))
        if identity not in seen:
            observations.append(observation)
            seen.add(identity)
    target.engine_observations = tuple(observations)


def _merge_peer(target: PeerStructure, incoming: PeerStructure) -> None:
    """Merge one canonical-geometry duplicate without synthesizing metrics.

    Conflicts retain the deterministic first value and become diagnostics;
    observations and derivation lineage remain deduplicated provenance.
    """

    target.discovered_by = tuple(sorted(
        set(target.discovered_by + incoming.discovered_by),
        key=lambda engine: (_DISCOVERY_ORDER.get(engine, 99), engine)))
    for attribute in ("dg_p3", "tm_p3_c", "dg_vienna", "tm_vienna_c"):
        current = getattr(target, attribute)
        candidate = getattr(incoming, attribute)
        if current is None and candidate is not None:
            setattr(target, attribute, candidate)
        elif current is not None and candidate is not None and current != candidate:
            target.geometry_diagnostics += (
                f"duplicate_metric:{attribute}; retained deterministic first value",
            )
    _merge_peer_observations(target, incoming)
    target.geometry_diagnostics = tuple(dict.fromkeys(
        target.geometry_diagnostics + incoming.geometry_diagnostics))
    lineage = list(target.derivation_lineage)
    seen_lineage = {repr(item) for item in lineage}
    for item in incoming.derivation_lineage:
        identity = repr(item)
        if identity not in seen_lineage:
            lineage.append(copy.deepcopy(item))
            seen_lineage.add(identity)
    target.derivation_lineage = tuple(lineage)
    if not target.structure and incoming.structure:
        target.structure = incoming.structure


def _finite_peer_metrics(
    peer: PeerStructure, *, hairpin: bool,
) -> tuple[list[tuple[float, str]], list[tuple[float, str]]]:
    dg_values: list[tuple[float, str]] = []
    tm_values: list[tuple[float, str]] = []
    for engine, values in peer.metrics_by_engine.items():
        dg = sm.finite_or_none(
            values["dg_kcal_mol"], engine=engine, quantity="structure_dg",
            diagnostics=_diagnostics(), none_is_diagnostic=False)
        tm = sm.finite_or_none(
            values["tm_c"], engine=engine, quantity="structure_tm",
            diagnostics=_diagnostics(), none_is_diagnostic=False)
        if dg is not None and (hairpin or engine != "seqfold"):
            dg_values.append((dg, engine))
        if hairpin and tm is not None:
            if engine in {"RNAstructure", "seqfold"}:
                observations = [item for item in peer.engine_observations
                                if item.engine == engine and item.tm_c == tm]
                if not any(item.tm_status == "crossing_found"
                           for item in observations):
                    continue
            tm_values.append((tm, engine))
    return dg_values, tm_values


def _has_only_positive_supported_dg(peer: PeerStructure) -> bool:
    """Return whether every supported finite exact-geometry dG is positive.

    Missing, failed, and unsupported engine values do not participate. The
    metric selector excludes seqfold for dimers because it has no dimer model.
    """

    dg_values, _tm_values = _finite_peer_metrics(
        peer, hairpin=peer.kind == "Hairpin")
    return bool(dg_values) and all(value > 0.0 for value, _engine in dg_values)


_NEAR_DUPLICATE_BOND_DIFFERENCE = 4
DIMER_DERIVATION_CANDIDATE_CAP = 128
DIMER_DERIVATION_SCORE_BATCH_LIMIT = 128
DIMER_GAP_DERIVATION_POLICY = "whole-stacked-block-subgeometry-v1"


@dataclass(frozen=True)
class DimerGapProfile:
    """Internal alignment gaps derived only from canonical dimer bonds."""

    measurable: bool
    total: int
    longest_run: int
    diagnostic: str = ""


def _dimer_gap_profile(geometry: ee.CanonicalGeometry) -> DimerGapProfile:
    """Measure internal display gaps without counting terminal padding.

    Strand B is converted from its input 5'-to-3' indices to the displayed
    3'-to-5' orientation.  Only intervals between consecutive paired anchors
    participate, making the result independent of arbitrary whole-strand
    padding in an ASCII rendering.
    """

    if geometry.kind == "hairpin" or geometry.sequence_b is None:
        return DimerGapProfile(
            False, 0, 0, "dimer gap profile requires two strands")
    _kind, _sequence_a, sequence_b, canonical_pairs = geometry.identity
    anchors = sorted(
        (left, len(sequence_b) - 1 - right)
        for left, right in canonical_pairs)
    if any(next_b <= current_b
           for (_current_a, current_b), (_next_a, next_b)
           in zip(anchors, anchors[1:])):
        return DimerGapProfile(
            False, 0, 0,
            "dimer gap profile unavailable: paired anchors are nonmonotonic "
            "in display orientation")
    runs = [
        abs((next_a - current_a - 1) - (next_b - current_b - 1))
        for (current_a, current_b), (next_a, next_b)
        in zip(anchors, anchors[1:])
    ]
    return DimerGapProfile(
        True, sum(runs), max(runs, default=0))


def _dimer_stacked_blocks(
    geometry: ee.CanonicalGeometry,
) -> tuple[tuple[tuple[int, int], ...], ...]:
    """Return maximal antiparallel stacks without splitting source helices."""

    sequence_b = geometry.sequence_b
    if sequence_b is None:
        return ()
    anchors = sorted(
        (left, len(sequence_b) - 1 - right, (left, right))
        for left, right in geometry.pairs)
    if not anchors:
        return ()
    blocks: list[list[tuple[int, int]]] = [[anchors[0][2]]]
    previous_a, previous_b, _pair = anchors[0]
    for left, displayed_right, pair in anchors[1:]:
        if (left, displayed_right) == (previous_a + 1, previous_b + 1):
            blocks[-1].append(pair)
        else:
            blocks.append([pair])
        previous_a, previous_b = left, displayed_right
    return tuple(tuple(block) for block in blocks)


def _record_derivation_failure(code: str, message: str) -> None:
    diagnostics = _diagnostics()
    if diagnostics is not None:
        diagnostics.append(sm.ScientificDiagnostic(
            code, "structure_policy", "dimer_geometry", message))


def _dimer_derivation_geometries(
    geometry: ee.CanonicalGeometry, cond: ReactionConditions,
) -> tuple[ee.CanonicalGeometry, ...] | None:
    """Enumerate the bounded maximum-retention whole-stack repair set.

    ``None`` means the parent cannot be repaired safely and must fail closed;
    an empty tuple means the measurable parent already satisfies the policy.
    """

    profile = _dimer_gap_profile(geometry)
    if not profile.measurable:
        _record_derivation_failure(
            "dimer_geometry_nonmonotonic",
            f"{profile.diagnostic}; structure omitted by dimer gap policy")
        return None
    if (profile.longest_run <= cond.dimer_max_consecutive_gaps
            and profile.total <= cond.dimer_max_total_gaps):
        return ()
    blocks = tuple(
        block for block in _dimer_stacked_blocks(geometry) if len(block) > 1)
    candidate_count = (1 << len(blocks)) - 1
    if candidate_count > DIMER_DERIVATION_CANDIDATE_CAP:
        _record_derivation_failure(
            "dimer_geometry_derivation_candidate_cap", (
                "dimer geometry derivation candidate cap exceeded: "
                f"{candidate_count} candidates exceeds immutable limit "
                f"{DIMER_DERIVATION_CANDIDATE_CAP}"))
        return None
    valid: list[ee.CanonicalGeometry] = []
    for subset_size in range(1, len(blocks) + 1):
        for selected in combinations(blocks, subset_size):
            pairs = tuple(sorted(
                pair for block in selected for pair in block))
            candidate = ee.CanonicalGeometry(
                geometry.kind, geometry.sequence_a, geometry.sequence_b, pairs)
            candidate_profile = _dimer_gap_profile(candidate)
            if (candidate_profile.measurable
                    and candidate_profile.longest_run
                    <= cond.dimer_max_consecutive_gaps
                    and candidate_profile.total
                    <= cond.dimer_max_total_gaps):
                valid.append(candidate)
    if not valid:
        _record_derivation_failure(
            "dimer_geometry_derivation_unavailable", (
                "dimer geometry has no whole-stacked-block subgeometry that "
                "satisfies the configured gap limits"))
        return None
    maximum_pairs = max(len(candidate.pairs) for candidate in valid)
    return tuple(sorted(
        (candidate for candidate in valid
         if len(candidate.pairs) == maximum_pairs),
        key=lambda candidate: repr(candidate.identity)))


def _derived_peer(
    parent: PeerStructure, geometry: ee.CanonicalGeometry,
    vienna_dg: float | None,
    rna_observation: ee.EngineObservation | None,
) -> PeerStructure:
    before = _dimer_gap_profile(parent.geometry)
    after = _dimer_gap_profile(geometry)
    removed = tuple(sorted(set(parent.geometry.pairs) - set(geometry.pairs)))
    lineage = {
        "policy": DIMER_GAP_DERIVATION_POLICY,
        "parent_identity": parent.geometry.identity,
        "parent_sources": tuple(parent.discovered_by),
        "removed_pairs": removed,
        "before_profile": {
            "total": before.total, "longest_run": before.longest_run},
        "after_profile": {
            "total": after.total, "longest_run": after.longest_run},
    }
    observation_tuple = (() if rna_observation is None
                         else (rna_observation,))
    return PeerStructure(
        kind=parent.kind, label=parent.label, found=True, geometry=geometry,
        structure="", discovered_by=(), dg_p3=None, tm_p3_c=None,
        dg_vienna=vienna_dg,
        n_combos=parent.n_combos,
        representative_variant=parent.representative_variant,
        involves_3prime=_geometry_involves_3prime(geometry),
        engine_observations=observation_tuple,
        thermo_model="Policy-constrained exact dimer subgeometry",
        pair_classes=geometry.pair_classes,
        geometry_diagnostics=_pair_class_diagnostics(geometry),
        derivation_lineage=(lineage,),
    )


def _derive_dimer_gap_constrained_peers(
    peers: list[PeerStructure], cond: ReactionConditions, *,
    score_rnastructure: bool,
    rnastructure_session: ee.RNAstructureSession | None = None,
    cancel_event: CancelEvent | None = None,
) -> list[PeerStructure]:
    """Replace failing monotonic parents with one exact rescored subgeometry."""
    retained, planned = _plan_dimer_gap_derivations(peers, cond)
    ordered_candidates = _ordered_unique_derivation_candidates(planned)
    vienna_by_identity, rna_by_identity = _score_dimer_derivation_candidates(
        ordered_candidates, cond, score_rnastructure=score_rnastructure,
        rnastructure_session=rnastructure_session, cancel_event=cancel_event)
    for parent, candidates in planned:
        retained.append(_select_adverse_dimer_derivation(
            parent, candidates, vienna_by_identity, rna_by_identity))
    return retained


def _plan_dimer_gap_derivations(
        peers: list[PeerStructure], cond: ReactionConditions
) -> tuple[list[PeerStructure], list[
        tuple[PeerStructure, tuple[ee.CanonicalGeometry, ...]]]]:
    retained: list[PeerStructure] = []
    planned: list[
        tuple[PeerStructure, tuple[ee.CanonicalGeometry, ...]]
    ] = []
    for parent in peers:
        geometry = parent.geometry
        if geometry is None or parent.kind == "Hairpin":
            retained.append(parent)
            continue
        profile = _dimer_gap_profile(geometry)
        if not profile.measurable:
            _record_derivation_failure(
                "dimer_geometry_nonmonotonic",
                f"{profile.diagnostic}; structure omitted by dimer gap policy")
            continue
        candidates = _dimer_derivation_geometries(geometry, cond)
        if candidates == ():
            retained.append(parent)
            continue
        if candidates is None:
            continue
        planned.append((parent, candidates))
    return retained, planned


def _ordered_unique_derivation_candidates(planned) -> tuple:
    unique_candidates: dict[tuple[object, ...], ee.CanonicalGeometry] = {}
    for _parent, candidates in planned:
        for candidate in candidates:
            unique_candidates.setdefault(candidate.identity, candidate)
    return tuple(
        unique_candidates[identity]
        for identity in sorted(unique_candidates, key=repr))


def _score_dimer_derivation_candidates(
        ordered_candidates: tuple[ee.CanonicalGeometry, ...],
        cond: ReactionConditions, *, score_rnastructure: bool,
        rnastructure_session: ee.RNAstructureSession | None,
        cancel_event: CancelEvent | None,
) -> tuple[dict[tuple[object, ...], float | None],
           dict[tuple[object, ...], ee.EngineObservation]]:
    vienna_by_identity: dict[tuple[object, ...], float | None] = {}
    for candidate in ordered_candidates:
        vienna_by_identity[candidate.identity] = (
            None if "mismatch" in candidate.pair_classes
            else vb.dimer_structure_dg(
                candidate.sequence_a, candidate.sequence_b or "",
                candidate.pairs, cond, _diagnostics()))

    rna_by_identity: dict[tuple[object, ...], ee.EngineObservation] = {}
    rna_candidates = tuple(
        candidate for candidate in ordered_candidates
        if "mismatch" not in candidate.pair_classes)
    if score_rnastructure:
        for start in range(0, len(rna_candidates),
                           DIMER_DERIVATION_SCORE_BATCH_LIMIT):
            candidate_batch = rna_candidates[
                start:start + DIMER_DERIVATION_SCORE_BATCH_LIMIT]
            batch = _safe_external_batch(
                lambda: ee.score_rnastructure_fixed_geometries(
                    candidate_batch, cond, session=rnastructure_session,
                    cancel_event=cancel_event))
            for diagnostic in batch.diagnostics:
                _record_derivation_failure(
                    "dimer_geometry_derivation_rnastructure", diagnostic)
            for observation in batch.observations:
                if observation.status == "ok" and observation.geometry is not None:
                    rna_by_identity[observation.geometry.identity] = observation
    return vienna_by_identity, rna_by_identity


def _select_adverse_dimer_derivation(
        parent: PeerStructure,
        candidates: tuple[ee.CanonicalGeometry, ...],
        vienna_by_identity: dict[tuple[object, ...], float | None],
        rna_by_identity: dict[tuple[object, ...], ee.EngineObservation],
) -> PeerStructure:
    derived = [
        _derived_peer(
            parent, candidate,
            vienna_by_identity.get(candidate.identity),
            rna_by_identity.get(candidate.identity))
        for candidate in candidates
    ]
    derived.sort(key=lambda peer: (
        min((value for value, _engine in _finite_peer_metrics(
            peer, hairpin=False)[0]), default=math.inf),
        repr(peer.geometry.identity)))
    return derived[0]


def _canonical_bond_identities(
    geometry: ee.CanonicalGeometry,
) -> frozenset[tuple[int, int]]:
    """Return bond identities in canonical strand orientation."""

    return frozenset(geometry.identity[3])


def _logical_bond_identities(
    geometry: ee.CanonicalGeometry,
) -> frozenset[tuple[int, int]]:
    """Return bonds in stable oligo-participant orientation.

    Heterodimer coordinates retain the caller's A/B participant order rather
    than following concrete-sequence lexical order. Self-dimers remain
    strand-swap symmetric because both strands represent the same participant.
    """

    direct = geometry.pairs
    if geometry.kind != "self-dimer":
        return frozenset(direct)
    swapped = tuple(sorted((right, left) for left, right in direct))
    return frozenset(min(direct, swapped))


def _near_duplicate_risk_score(peer: PeerStructure) -> Decimal | None:
    """Return the exact-geometry risk metric at its decision resolution."""

    dgs, tms = _finite_peer_metrics(peer, hairpin=peer.kind == "Hairpin")
    if peer.kind == "Hairpin":
        if not tms:
            return None
        return sm.quantize_decimal(
            max(value for value, _engine in tms),
            sm.TM_DECISION_RESOLUTION)
    if not dgs:
        return None
    return sm.quantize_decimal(
        min(value for value, _engine in dgs), sm.DG_DECISION_RESOLUTION)


def _near_duplicate_bond_set(
    peer: PeerStructure, *, logical: bool,
) -> frozenset[tuple[int, int]]:
    """Project one peer's bonds into the requested comparison coordinate set."""

    if peer.geometry is None:
        return frozenset()
    if logical:
        return _logical_bond_identities(peer.geometry)
    return _canonical_bond_identities(peer.geometry)


def _near_duplicate_strand_lengths(
    geometry: ee.CanonicalGeometry, *, logical: bool,
) -> tuple[int, int]:
    """Return strand lengths in logical or canonical comparison order."""

    if logical:
        return len(geometry.sequence_a), len(geometry.sequence_b or "")
    return tuple(len(sequence) for sequence in geometry.identity[1:3])


def _near_duplicate_peers_are_comparable(
    peer: PeerStructure,
    other: PeerStructure,
    *,
    logical: bool,
) -> bool:
    """Apply the kind, site, strand-length, and concrete-context gates."""

    if other.kind != peer.kind or other.involves_3prime != peer.involves_3prime:
        return False
    geometry = peer.geometry
    other_geometry = other.geometry
    if geometry is None or other_geometry is None:
        return False
    if _near_duplicate_strand_lengths(geometry, logical=logical) != (
            _near_duplicate_strand_lengths(other_geometry, logical=logical)):
        return False
    return logical or geometry.identity[:3] == other_geometry.identity[:3]


def _near_duplicate_other_dominates(
    peer: PeerStructure,
    score: Decimal,
    other: PeerStructure,
    other_score: Decimal,
    *,
    logical: bool,
) -> bool:
    """Apply adverse-score ordering and deterministic logical tie breaking."""

    other_is_more_adverse = (
        other_score > score if peer.kind == "Hairpin" else other_score < score)
    return other_is_more_adverse or (
        logical
        and other_score == score
        and _peer_total_order_key(other) < _peer_total_order_key(peer)
    )


def _suppress_near_duplicate_peers(
    peers: list[PeerStructure],
    bond_difference_exclusive_maximum: int = _NEAR_DUPLICATE_BOND_DIFFERENCE,
    *, comparison_scope: str = "concrete_context",
) -> list[PeerStructure]:
    """Omit directly dominated near-duplicates without transitive collapse.

    Peers are comparable only when kind, 3'-site classification, and strand
    lengths match; concrete-context comparison additionally requires identical
    sequences. A bond-set symmetric difference strictly below the configured
    threshold is near-duplicate. Higher hairpin Tm or lower dimer dG dominates;
    missing risk metrics never do. Equal-score peers are retained within one
    concrete context, while logical-interaction ties use the deterministic peer
    order to keep exactly one. Every decision compares the original peer set,
    preventing a chain of small differences from collapsing transitively.
    """

    if comparison_scope not in {"concrete_context", "logical_interaction"}:
        raise ValueError(f"unknown near-duplicate comparison scope: {comparison_scope}")
    original = tuple(peers)
    scores = tuple(_near_duplicate_risk_score(peer) for peer in original)
    logical = comparison_scope == "logical_interaction"
    bond_sets = tuple(
        _near_duplicate_bond_set(peer, logical=logical) for peer in original)
    retained: list[PeerStructure] = []
    for index, peer in enumerate(original):
        geometry = peer.geometry
        score = scores[index]
        if geometry is None or score is None:
            retained.append(peer)
            continue
        dominated = False
        for other_index, other in enumerate(original):
            if other_index == index or not _near_duplicate_peers_are_comparable(
                    peer, other, logical=logical):
                continue
            other_score = scores[other_index]
            if other_score is None:
                continue
            if len(bond_sets[index] ^ bond_sets[other_index]) >= (
                    bond_difference_exclusive_maximum):
                continue
            if _near_duplicate_other_dominates(
                    peer, score, other, other_score, logical=logical):
                dominated = True
                break
        if not dominated:
            retained.append(peer)
    return retained


def _rank_peer_structures(
    peers: list[PeerStructure], kind: str, label: str, *,
    cond: ReactionConditions,
    exclude_all_positive_dg: bool,
    suppress_near_duplicates: bool = True,
    near_duplicate_scope: str = "concrete_context",
) -> list[PeerStructure]:
    """Apply the visibility policy and assign contiguous deterministic ranks."""

    if exclude_all_positive_dg:
        peers = [peer for peer in peers
                 if not _has_only_positive_supported_dg(peer)]
    if suppress_near_duplicates:
        if near_duplicate_scope == "concrete_context":
            peers = _suppress_near_duplicate_peers(
                peers, cond.near_duplicate_bond_difference)
        else:
            peers = _suppress_near_duplicate_peers(
                peers, cond.near_duplicate_bond_difference,
                comparison_scope=near_duplicate_scope)
    peers.sort(key=_peer_total_order_key)
    for rank, peer in enumerate(peers, 1):
        peer.rank = rank
        peer.label = f"{kind}: {label} #{rank}"
    return peers


def _score_peer_vienna(peer: PeerStructure, cond: ReactionConditions) -> None:
    geometry = peer.geometry
    if geometry is None:
        return
    if peer.kind == "Hairpin":
        pattern = _geometry_hairpin_pattern(geometry)
        if peer.dg_vienna is None:
            peer.dg_vienna = vb.hairpin_structure_dg(
                geometry.sequence_a, pattern, cond, _diagnostics())
        if peer.tm_vienna_c is None:
            peer.tm_vienna_c = _fixed_tm_value(vb.hairpin_structure_tm(
                geometry.sequence_a, pattern, cond, _diagnostics()))
    elif peer.dg_vienna is None and geometry.sequence_b is not None:
        peer.dg_vienna = vb.dimer_structure_dg(
            geometry.sequence_a, geometry.sequence_b, geometry.pairs,
            cond, _diagnostics())


def _peer_pair_count(peer: PeerStructure) -> int:
    return len(peer.geometry.pairs) if peer.geometry is not None else 0


def _peer_total_order_key(peer: PeerStructure) -> tuple:
    dgs, tms = _finite_peer_metrics(peer, hairpin=peer.kind == "Hairpin")
    minimum_dg = (sm.quantize_decimal(
        min(value for value, _engine in dgs), sm.DG_DECISION_RESOLUTION)
        if dgs else Decimal("Infinity"))
    maximum_tm = (sm.quantize_decimal(
        max(value for value, _engine in tms), sm.TM_DECISION_RESOLUTION)
        if tms else Decimal("-Infinity"))
    common = (
        _SEVERITY_ORDER.get(peer.severity, 3),
        not peer.involves_3prime,
        -_peer_pair_count(peer),
        repr(peer.geometry.identity if peer.geometry else ()),
        peer.representative_variant,
    )
    if peer.kind == "Hairpin":
        return (common[0], -maximum_tm, minimum_dg, *common[1:])
    return (common[0], minimum_dg, *common[1:])


def _classify_peer(peer: PeerStructure, cond: ReactionConditions) -> None:
    hairpin = peer.kind == "Hairpin"
    dgs, tms = _finite_peer_metrics(peer, hairpin=hairpin)
    dg_severity = "ok"
    dg_fragment = "no finite exact-geometry dG"
    if dgs:
        minimum_dg, dg_engine = min(dgs, key=lambda item: (item[0], item[1]))
        dg_severity, _level_name = _level(minimum_dg, cond)
        dg_fragment = f"min dG {minimum_dg:.2f} kcal/mol ({dg_engine})"
    if hairpin:
        pair_count = _peer_pair_count(peer)
        maximum_tm = max(tms, default=(None, ""), key=lambda item: item[0])
        tm_value = maximum_tm[0]
        tm_severity = _hairpin_tm_severity(tm_value, pair_count)
        if not dgs and tm_value is None and pair_count < 7:
            peer.severity = "unclassified"
        else:
            peer.severity = _max_severity(dg_severity, tm_severity)
        tm_fragment = (
            f"max Tm {tm_value:.2f} C ({maximum_tm[1]})"
            if tm_value is not None else "exact-geometry Tm unavailable")
        peer.assessment = (
            f"{dg_fragment}; {tm_fragment}; {pair_count} paired bases - "
            f"{_tail_for_severity(peer.severity) if peer.severity != 'unclassified' else 'not classified'}")
    else:
        peer.severity = dg_severity if dgs else "unclassified"
        peer.assessment = (
            f"{dg_fragment} - "
            f"{_tail_for_severity(peer.severity) if dgs else 'not classified'}")
    if peer.n_combos > 1:
        peer.assessment += (
            f" [representative of {peer.n_combos} variant combinations; "
            "non-Primer3 engines cover the Primer3-selected variant only]")


@dataclass(frozen=True)
class _PeerFinalizationRequest:
    kind: str
    label: str
    sequence_a: str
    sequence_b: str | None
    candidates: tuple
    cond: ReactionConditions
    batch: ee.EngineBatch | None
    score_fixed_metrics: bool
    exclude_all_positive_dg: bool
    suppress_near_duplicates: bool
    apply_gap_derivation: bool
    rnastructure_session: ee.RNAstructureSession | None
    cancel_event: CancelEvent | None


def _finalize_peer_structures(
    kind: str, label: str, sequence_a: str, sequence_b: str | None,
    candidates, cond: ReactionConditions, *, batch: ee.EngineBatch | None = None,
    score_fixed_metrics: bool = False,
    exclude_all_positive_dg: bool = True,
    suppress_near_duplicates: bool = True,
    apply_gap_derivation: bool = True,
    rnastructure_session: ee.RNAstructureSession | None = None,
    cancel_event: CancelEvent | None = None,
) -> list[PeerStructure]:
    """Compatibility facade for exact peer finalization."""

    return _finalize_peer_structures_request(_PeerFinalizationRequest(
        kind=kind, label=label, sequence_a=sequence_a, sequence_b=sequence_b,
        candidates=tuple(candidates), cond=cond, batch=batch,
        score_fixed_metrics=score_fixed_metrics,
        exclude_all_positive_dg=exclude_all_positive_dg,
        suppress_near_duplicates=suppress_near_duplicates,
        apply_gap_derivation=apply_gap_derivation,
        rnastructure_session=rnastructure_session,
        cancel_event=cancel_event,
    ))


def _finalize_peer_structures_request(
    request: _PeerFinalizationRequest,
) -> list[PeerStructure]:
    """Deduplicate, exact-score, classify and deterministically rank peers."""
    peers = _merge_peer_candidates(request)
    peers = _apply_peer_gap_derivation(peers, request)
    _attach_peer_final_state(peers, request)
    return _rank_peer_structures(
        peers, request.kind, request.label, cond=request.cond,
        exclude_all_positive_dg=request.exclude_all_positive_dg,
        suppress_near_duplicates=request.suppress_near_duplicates)


def _normalized_peer_candidate(
        raw: PeerStructure, request: _PeerFinalizationRequest
) -> PeerStructure | None:
    # Geometry and observations are immutable value records; cloning their
    # full object graphs for every candidate dominated Python finalization.
    peer = copy.copy(raw)
    peer.external_metric_states = copy.deepcopy(raw.external_metric_states)
    peer.derivation_lineage = copy.deepcopy(raw.derivation_lineage)
    for observation in peer.engine_observations:
        if observation.status == "parse_error" and observation.warning:
            peer.geometry_diagnostics += (observation.warning,)
    for attribute, engine, quantity in (
        ("dg_p3", "Primer3", "dG"),
        ("tm_p3_c", "Primer3", "Tm"),
        ("dg_vienna", "ViennaRNA", "dG"),
        ("tm_vienna_c", "ViennaRNA", "Tm"),
    ):
        raw_value = getattr(peer, attribute)
        normalized = sm.finite_or_none(
            raw_value, engine=engine, quantity=quantity,
            diagnostics=_diagnostics(), none_is_diagnostic=False)
        if raw_value is not None and normalized is None:
            peer.geometry_diagnostics += (
                f"nonfinite/non-numeric {engine} {quantity} discarded",)
        setattr(peer, attribute, normalized)
    return _orient_peer_candidate(peer, request)


def _orient_peer_candidate(
        peer: PeerStructure, request: _PeerFinalizationRequest
) -> PeerStructure | None:
    geometry = peer.geometry
    if not peer.found or geometry is None or not geometry.pairs:
        return None
    direct = (geometry.sequence_a == request.sequence_a
              and geometry.sequence_b == request.sequence_b)
    swapped = (request.sequence_b is not None
               and geometry.sequence_a == request.sequence_b
               and geometry.sequence_b == request.sequence_a)
    if not direct and not swapped:
        return None
    if swapped:
        geometry = ee.CanonicalGeometry(
            geometry.kind, request.sequence_a, request.sequence_b,
            tuple((right, left) for left, right in geometry.pairs))
        peer.geometry = geometry
        peer.pair_classes = geometry.pair_classes
    return peer


def _merge_peer_candidates(
        request: _PeerFinalizationRequest) -> list[PeerStructure]:
    merged: dict[tuple[object, ...], PeerStructure] = {}
    ordered_candidates = sorted(request.candidates, key=lambda peer: (
        repr(peer.geometry.identity if peer.geometry else ()),
        repr(tuple(peer.discovered_by)),
        repr((peer.dg_p3, peer.tm_p3_c, peer.dg_vienna, peer.tm_vienna_c)),
        ee.observations_json(peer.engine_observations),
    ))
    for raw in ordered_candidates:
        peer = _normalized_peer_candidate(raw, request)
        if peer is None:
            continue
        geometry = peer.geometry
        assert geometry is not None
        target = merged.get(geometry.identity)
        if target is None:
            merged[geometry.identity] = peer
        else:
            _merge_peer(target, peer)
    return list(merged.values())


def _apply_peer_gap_derivation(
        peers: list[PeerStructure], request: _PeerFinalizationRequest
) -> list[PeerStructure]:
    if request.kind == "Hairpin" or not request.apply_gap_derivation:
        return peers
    peers = _derive_dimer_gap_constrained_peers(
        peers, request.cond,
        score_rnastructure=(request.batch is not None
                            or request.rnastructure_session is not None),
        rnastructure_session=request.rnastructure_session,
        cancel_event=request.cancel_event)
    final_merged: dict[tuple[object, ...], PeerStructure] = {}
    for peer in sorted(peers, key=lambda item: (
            bool(item.derivation_lineage),
            repr(item.geometry.identity if item.geometry else ()),
            repr(item.discovered_by))):
        assert peer.geometry is not None
        target = final_merged.get(peer.geometry.identity)
        if target is None:
            final_merged[peer.geometry.identity] = peer
        else:
            _merge_peer(target, peer)
    return list(final_merged.values())


def _attach_peer_final_state(
        peers: list[PeerStructure], request: _PeerFinalizationRequest) -> None:
    for peer in peers:
        peer.involves_3prime = _geometry_involves_3prime(peer.geometry)
        if request.score_fixed_metrics:
            _score_peer_vienna(peer, request.cond)
        _classify_peer(peer, request.cond)
        if request.batch is not None:
            peer.external_metric_states = _external_metric_states(
                peer, request.batch,
                "hairpin" if request.kind == "Hairpin" else "dimer")


def _terminus_3prime(structure: str, kind: str) -> bool:
    """Apply the site policy to Primer3 ASCII pairing.

    Check the terminal base for a hairpin, or either of the final two bases
    of either strand for a dimer."""
    tagged = _tagged_rows(structure)
    seq_rows = [c for t, c in tagged if t == "SEQ"]
    str_rows = [c for t, c in tagged if t == "STR"]
    if kind == "Hairpin":
        pat = _hairpin_pattern(structure)
        return bool(pat) and pat[-1] != "-"
    width = max((len(c) for c in seq_rows + str_rows), default=0)
    if not width:
        return False
    seq_rows = [c.ljust(width) for c in seq_rows]
    str_rows = [c.ljust(width) for c in str_rows]
    inner_top, inner_bot = seq_rows[-1], str_rows[0]

    def _cols(rows):
        return [c for c in range(width) if any(r[c] not in " -" for r in rows)]

    def _paired(c):
        return (c is not None and inner_top[c] not in " -"
                and inner_bot[c] not in " -")

    top_cols, bot_cols = _cols(seq_rows), _cols(str_rows)
    # top strand runs 5'->3' left-to-right (3' end at right); bottom is
    # antiparallel (its 3' end sits at the left).
    top_risk_cols = top_cols[-2:] if top_cols else []
    bot_risk_cols = bot_cols[:2] if bot_cols else []
    return any(_paired(c) for c in (*top_risk_cols, *bot_risk_cols))


def _worst_over(records) -> tuple:
    """From an iterable of (found, dg_kcal, tm, structure, variant_label) pick
    the worst case: any found structure beats none; among found, the most
    negative dG wins. Returns the chosen tuple (or a not-found sentinel)."""
    found_records = [rec for rec in records if rec[0]]
    best = min(found_records, key=lambda rec: (
        sm.quantize_decimal(rec[1], sm.DG_DECISION_RESOLUTION),
        str(rec[3]),
        str(rec[4]),
    )) if found_records else None
    if best is not None:
        return best
    return (False, 0.0, 0.0, "", "")


def _remember_primer3_record(record: tuple, context_id) -> tuple:
    """Keep calculated geometry metrics for later allocated context tasks."""
    records = _ACTIVE_PRIMER3_RECORDS.get()
    if (records is not None and context_id is not None
            and len(records) < _ACTIVE_PRIMER3_RECORD_LIMIT.get()):
        records[context_id] = record
    return record


def _hairpin_variant(seq, oligo, cond, *, coverage_variant_index=None):
    context_id = (
        None if coverage_variant_index is None
        else _coverage_context_id("hairpin", coverage_variant_index))
    try:
        r = primer3.calc_hairpin(
            seq, mv_conc=cond.mv_conc, dv_conc=cond.dv_conc,
            dntp_conc=cond.dntp_conc, dna_conc=oligo.conc_nM,
            temp_c=cond.dg_temp_c, output_structure=True)
    except Exception as exc:
        diagnostics = _diagnostics()
        if diagnostics is not None:
            diagnostics.append(sm.exception_diagnostic(
                "Primer3", "hairpin", exc))
        _record_coverage_outcome(
            "Primer3", "hairpin", "calculation_failed",
            context_id=context_id)
        return (False, 0.0, 0.0, "", seq)
    if not r.structure_found:
        _record_coverage_outcome(
            "Primer3", "hairpin", "no_structure",
            context_id=context_id)
        return (False, 0.0, 0.0, "", seq)
    dg = finite_or_none(r.dg, engine="Primer3", quantity="hairpin_dg")
    tm = finite_or_none(r.tm, engine="Primer3", quantity="hairpin_tm")
    if dg is None:
        _record_coverage_outcome(
            "Primer3", "hairpin", "calculation_failed",
            context_id=context_id)
        return (False, 0.0, tm or 0.0, "", seq)
    _record_coverage_outcome(
        "Primer3", "hairpin", "ok", context_id=context_id)
    return _remember_primer3_record(
        (True, _cal_to_kcal(dg), tm, r.ascii_structure or "", seq), context_id)


# --- local-structure enumeration ------------------------------------------- #
def _fixed_tm_value(result: object) -> float | None:
    """Accept the public status result and legacy numeric test doubles."""

    if isinstance(result, vb.FixedStructureTmResult):
        if result.status != "crossing_found":
            return None
        return sm.finite_or_none(
            result.value, engine="ViennaRNA", quantity="hairpin_structure_tm",
            diagnostics=_diagnostics(), none_is_diagnostic=False)
    return sm.finite_or_none(
        result, engine="ViennaRNA", quantity="hairpin_structure_tm",
        diagnostics=_diagnostics(), none_is_diagnostic=False)


def _primer3_peer(
    kind: str, label: str, found: bool, dg: float, tm: float,
    structure: str, sequence_a: str, sequence_b: str | None,
    n_combos: int, representative_variant: str,
) -> PeerStructure | None:
    if not found:
        return None
    try:
        if kind == "Hairpin":
            pattern = _hairpin_pattern(structure)
            opens = [index for index, marker in enumerate(pattern) if marker == "/"]
            closes = [index for index, marker in enumerate(pattern) if marker == "\\"]
            geometry = ee.CanonicalGeometry(
                "hairpin", sequence_a, None, tuple(zip(opens, reversed(closes))))
        else:
            identity = _dimer_identity_from_structure(structure)
            if identity is None:
                return None
            _tag, parsed_a, displayed_b, _offset, displayed_pairs = identity
            input_b = displayed_b[::-1]
            geometry = ee.CanonicalGeometry(
                "self-dimer" if kind == "Self-dimer" else "hetero-dimer",
                parsed_a, input_b,
                tuple((left, len(displayed_b) - 1 - right)
                      for left, right in displayed_pairs))
        return PeerStructure(
            kind=kind, label=label, found=True, geometry=geometry,
            structure=structure, discovered_by=("Primer3",),
            dg_p3=dg, tm_p3_c=tm,
            n_combos=n_combos, representative_variant=representative_variant,
            involves_3prime=_terminus_3prime(structure, kind),
            thermo_model="Primer3 selected structure",
            pair_classes=geometry.pair_classes,
            geometry_diagnostics=_pair_class_diagnostics(geometry),
        )
    except ValueError as exc:
        diagnostics = _diagnostics()
        if diagnostics is not None:
            diagnostics.append(sm.ScientificDiagnostic(
                "invalid_geometry", "Primer3", kind, str(exc)))
        return None


def analyze_hairpin(
    oligo: Oligo, cond: ReactionConditions, *, include_external: bool = True,
) -> InteractionResult:
    found, dg, tm, struct, var = _worst_over(
        _hairpin_variant(
            sequence, oligo, cond,
            coverage_variant_index=variant_index)
        for variant_index, sequence in enumerate(oligo.variants))
    rep = var or oligo.variants[0]
    representative_context_id = _coverage_context_id(
        "hairpin", oligo.variants.index(rep))
    candidates: list[PeerStructure] = []
    primer3_peer = _primer3_peer(
        "Hairpin", oligo.name, found, dg, tm, struct, rep, None,
        oligo.n_variants, rep)
    if primer3_peer is not None:
        candidates.append(primer3_peer)
    candidates.extend(peer for sub in _observed_discovery(
        "LocalEnumerator", "hairpin",
        lambda: nn.enumerate_hairpins(rep, cond, LOCAL_ENUMERATION_POOL),
        context_id=representative_context_id)
        if (peer := _peer_from_local(
            sub, oligo.name, oligo.n_variants, rep)) is not None)
    candidates.extend(peer for item in _observed_discovery(
        "ViennaRNA", "hairpin",
        lambda: vb.discover_hairpin_structures(
            rep, cond, VIENNA_MAX_STRUCTURES, _diagnostics()),
        context_id=representative_context_id)
        if (peer := _peer_from_vienna(
            item, "Hairpin", oligo.name, rep, None,
            oligo.n_variants, rep)) is not None)
    result = InteractionResult(
        "Hairpin", oligo.name, rep, None,
        n_combos=oligo.n_variants, representative_variant=rep)
    result.structures = _finalize_peer_structures(
        result.kind, result.label, rep, None, candidates, cond,
        score_fixed_metrics=True,
        exclude_all_positive_dg=not include_external,
        suppress_near_duplicates=False,
        apply_gap_derivation=not include_external)
    if include_external and not _DEFER_EXTERNAL.get():
        batch = _safe_external_batch(lambda: ee.run_hairpin_engines(
            rep, cond.for_conc(oligo.conc_nM)))
        return _merge_external_observations(
            result, batch, cond.for_conc(oligo.conc_nM))
    return result


def _homodimer_variant(seq, oligo, cond, *, coverage_variant_index=None):
    context_id = (
        None if coverage_variant_index is None
        else _coverage_context_id("self_dimer", coverage_variant_index))
    try:
        r = primer3.calc_homodimer(
            seq, mv_conc=cond.mv_conc, dv_conc=cond.dv_conc,
            dntp_conc=cond.dntp_conc, dna_conc=oligo.conc_nM,
            temp_c=cond.dg_temp_c, output_structure=True)
    except Exception as exc:
        diagnostics = _diagnostics()
        if diagnostics is not None:
            diagnostics.append(sm.exception_diagnostic(
                "Primer3", "homodimer", exc))
        _record_coverage_outcome(
            "Primer3", "self_dimer", "calculation_failed",
            context_id=context_id)
        return (False, 0.0, 0.0, "", seq)
    if not r.structure_found:
        _record_coverage_outcome(
            "Primer3", "self_dimer", "no_structure",
            context_id=context_id)
        return (False, 0.0, 0.0, "", seq)
    dg = finite_or_none(r.dg, engine="Primer3", quantity="homodimer_dg")
    tm = finite_or_none(r.tm, engine="Primer3", quantity="homodimer_tm")
    if dg is None:
        _record_coverage_outcome(
            "Primer3", "self_dimer", "calculation_failed",
            context_id=context_id)
        return (False, 0.0, tm or 0.0, "", seq)
    _record_coverage_outcome(
        "Primer3", "self_dimer", "ok", context_id=context_id)
    return _remember_primer3_record(
        (True, _cal_to_kcal(dg), tm, r.ascii_structure or "", seq), context_id)


def analyze_self_dimer(
    oligo: Oligo, cond: ReactionConditions, *, include_external: bool = True,
) -> InteractionResult:
    found, dg, tm, struct, var = _worst_over(
        _homodimer_variant(
            sequence, oligo, cond,
            coverage_variant_index=variant_index)
        for variant_index, sequence in enumerate(oligo.variants))
    rep = var or oligo.variants[0]
    representative_variant_index = oligo.variants.index(rep)
    representative_context_id = _coverage_context_id(
        "self_dimer", representative_variant_index)
    candidates: list[PeerStructure] = []
    primer3_peer = _primer3_peer(
        "Self-dimer", oligo.name, found, dg, tm, struct, rep, rep,
        oligo.n_variants, rep)
    if primer3_peer is not None:
        candidates.append(primer3_peer)
    candidates.extend(peer for sub in _observed_discovery(
        "LocalEnumerator", "self_dimer",
        lambda: nn.enumerate_duplex(
            rep, rep, cond, LOCAL_ENUMERATION_POOL, self_dimer=True),
        context_id=representative_context_id)
        if (peer := _peer_from_local(
            sub, oligo.name, oligo.n_variants, rep)) is not None)
    candidates.extend(peer for item in _observed_discovery(
        "ViennaRNA", "self_dimer",
        lambda: vb.discover_dimer_structures(
            rep, rep, cond, _diagnostics()),
        context_id=representative_context_id)
        if (peer := _peer_from_vienna(
            item, "Self-dimer", oligo.name, rep, rep,
            oligo.n_variants, rep)) is not None)
    result = InteractionResult(
        "Self-dimer", oligo.name, rep, rep,
        n_combos=oligo.n_variants, representative_variant=rep)
    result.structures = _finalize_peer_structures(
        result.kind, result.label, rep, rep, candidates, cond,
        score_fixed_metrics=True,
        exclude_all_positive_dg=not include_external,
        suppress_near_duplicates=False,
        apply_gap_derivation=not include_external)
    if include_external and not _DEFER_EXTERNAL.get():
        batch = _safe_external_batch(lambda: ee.run_dimer_engines(
            rep, rep, cond.for_conc(oligo.conc_nM),
            self_dimer=True))
        return _merge_external_observations(
            result, batch, cond.for_conc(oligo.conc_nM))
    return result


def _heterodimer_variant(
    sa, sb, dna_conc, cond, *,
    coverage_variant_indices: tuple[int, int] | None = None,
):
    context_id = (
        None if coverage_variant_indices is None else _coverage_context_id(
            "hetero_dimer", *coverage_variant_indices))
    label = f"{sa} / {sb}"
    try:
        r = primer3.calc_heterodimer(
            sa, sb, mv_conc=cond.mv_conc, dv_conc=cond.dv_conc,
            dntp_conc=cond.dntp_conc, dna_conc=dna_conc,
            temp_c=cond.dg_temp_c, output_structure=True)
    except Exception as exc:
        diagnostics = _diagnostics()
        if diagnostics is not None:
            diagnostics.append(sm.exception_diagnostic(
                "Primer3", "heterodimer", exc))
        _record_coverage_outcome(
            "Primer3", "hetero_dimer", "calculation_failed",
            context_id=context_id)
        return (False, 0.0, 0.0, "", label)
    if not r.structure_found:
        _record_coverage_outcome(
            "Primer3", "hetero_dimer", "no_structure",
            context_id=context_id)
        return (False, 0.0, 0.0, "", label)
    dg = finite_or_none(r.dg, engine="Primer3", quantity="heterodimer_dg")
    tm = finite_or_none(r.tm, engine="Primer3", quantity="heterodimer_tm")
    if dg is None:
        _record_coverage_outcome(
            "Primer3", "hetero_dimer", "calculation_failed",
            context_id=context_id)
        return (False, 0.0, tm or 0.0, "", label)
    _record_coverage_outcome(
        "Primer3", "hetero_dimer", "ok", context_id=context_id)
    return _remember_primer3_record(
        (True, _cal_to_kcal(dg), tm, r.ascii_structure or "", label), context_id)


def analyze_hetero(
    a: Oligo, b: Oligo, cond: ReactionConditions, *,
    include_external: bool = True,
) -> InteractionResult:
    dna_conc = max(a.conc_nM, b.conc_nM)
    combos = a.n_variants * b.n_variants
    found, dg, tm, struct, var = _worst_over(
        _heterodimer_variant(
            sequence_a, sequence_b, dna_conc, cond,
            coverage_variant_indices=(variant_a, variant_b))
        for variant_a, sequence_a in enumerate(a.variants)
        for variant_b, sequence_b in enumerate(b.variants))
    if var and " / " in var:
        sa, sb = var.split(" / ", 1)
    else:
        sa, sb = a.variants[0], b.variants[0]
    representative_context_id = _coverage_context_id(
        "hetero_dimer", a.variants.index(sa), b.variants.index(sb))
    label = f"{a.name} x {b.name}"
    candidates: list[PeerStructure] = []
    primer3_peer = _primer3_peer(
        "Hetero-dimer", label, found, dg, tm, struct, sa, sb, combos, var)
    if primer3_peer is not None:
        candidates.append(primer3_peer)
    candidates.extend(peer for sub in _observed_discovery(
        "LocalEnumerator", "hetero_dimer",
        lambda: nn.enumerate_duplex(
            sa, sb, cond, LOCAL_ENUMERATION_POOL),
        context_id=representative_context_id)
        if (peer := _peer_from_local(sub, label, combos, var)) is not None)
    candidates.extend(peer for item in _observed_discovery(
        "ViennaRNA", "hetero_dimer",
        lambda: vb.discover_dimer_structures(
            sa, sb, cond, _diagnostics()),
        context_id=representative_context_id)
        if (peer := _peer_from_vienna(
            item, "Hetero-dimer", label, sa, sb, combos, var)) is not None)
    result = InteractionResult(
        "Hetero-dimer", label, sa, sb, n_combos=combos,
        representative_variant=var)
    result.structures = _finalize_peer_structures(
        result.kind, result.label, sa, sb, candidates, cond,
        score_fixed_metrics=True,
        exclude_all_positive_dg=not include_external,
        suppress_near_duplicates=False,
        apply_gap_derivation=not include_external)
    if include_external and not _DEFER_EXTERNAL.get():
        batch = _safe_external_batch(lambda: ee.run_dimer_engines(
            sa, sb, cond.for_conc(dna_conc),
            self_dimer=False))
        return _merge_external_observations(
            result, batch, cond.for_conc(dna_conc))
    return result


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
@dataclass
class AnalysisReport:
    tms: list[TmResult] = field(default_factory=list)
    hairpins: list[InteractionResult] = field(default_factory=list)
    self_dimers: list[InteractionResult] = field(default_factory=list)
    hetero_dimers: list[InteractionResult] = field(default_factory=list)
    manifest: Optional[sm.ScientificManifest] = None
    diagnostics: tuple[sm.ScientificDiagnostic, ...] = field(
        default_factory=tuple)
    ensemble_plan: EnsembleWorkPlan | None = None
    ensemble_coverage: EnsembleCoverage | None = None
    complete: bool = True
    phase: str = "complete"

    @property
    def ensemble_complete(self) -> bool:
        return bool(self.ensemble_coverage
                    and self.ensemble_coverage.ensemble_complete)

    @property
    def all_interactions(self) -> list[InteractionResult]:
        return self.hairpins + self.self_dimers + self.hetero_dimers

    @property
    def all_structures(self) -> list[PeerStructure]:
        return [peer for interaction in self.all_interactions
                for peer in interaction.structures]

    @property
    def all_findings(self) -> list:
        return list(self.all_structures)

    @property
    def problems(self) -> list:
        return [s for s in self.all_findings if s.severity == "problem"]

    @property
    def cautions(self) -> list:
        return [s for s in self.all_findings if s.severity == "caution"]

    @property
    def has_degenerate(self) -> bool:
        return any(t.oligo.is_degenerate for t in self.tms)


@dataclass(frozen=True)
class ReportFinding:
    """Normalized row used by result navigation, filters, and matrix views."""

    structure: object
    group: str
    kind: str
    label: str
    rank: int
    discovered_by: tuple[str, ...]
    severity: str
    found: bool
    involves_3prime: bool
    dg_p3: Optional[float]
    dg_vienna: Optional[float]
    tm_c: Optional[float]
    assessment: str

    @property
    def source_rank(self) -> int:
        """Peer rank retained under the historic consumer attribute name."""
        return self.rank

    @property
    def engine_observations(self) -> tuple[ee.EngineObservation, ...]:
        return tuple(getattr(self.structure, "engine_observations", ()))

    @property
    def site(self) -> str:
        if not self.found:
            return "-"
        return "3' end" if self.involves_3prime else "internal"

    @property
    def effective_dg(self) -> Optional[float]:
        """Internal all-engine dG ordering key, not a report measurement."""
        values, _tms = _finite_peer_metrics(
            self.structure, hairpin=self.kind == "Hairpin")
        return min((value for value, _engine in values), default=None)

    @property
    def effective_dg_engine(self) -> Optional[str]:
        """Engine contributing the most-negative supported exact dG."""
        values, _tms = _finite_peer_metrics(
            self.structure, hairpin=self.kind == "Hairpin")
        return min(values, key=lambda item: (item[0], item[1]))[1] if values else None

@dataclass(frozen=True)
class HeteroMatrixCell:
    row_name: str
    column_name: str
    finding: ReportFinding


_SEVERITY_ORDER = {"problem": 0, "caution": 1, "ok": 2}


def _structure_group(kind: str) -> str:
    return {
        "Hairpin": "hairpins",
        "Self-dimer": "self_dimers",
        "Hetero-dimer": "hetero_dimers",
    }.get(kind, kind.lower())


def _finding_from_structure(s: PeerStructure, group: str) -> ReportFinding:
    found = getattr(s, "found", True)
    return ReportFinding(
        structure=s,
        group=group,
        kind=s.kind,
        label=s.label,
        rank=s.rank,
        discovered_by=s.discovered_by,
        severity=s.severity,
        found=found,
        involves_3prime=getattr(s, "involves_3prime", False),
        dg_p3=s.dg_kcal if found else None,
        dg_vienna=getattr(s, "dg_vienna", None),
        tm_c=getattr(s, "tm_c", None),
        assessment=getattr(s, "assessment", ""),
    )


def iter_report_findings(report: AnalysisReport) -> list[ReportFinding]:
    """Return every ranked peer structure in deterministic report order."""
    return [_finding_from_structure(peer, _structure_group(peer.kind))
            for interaction in report.all_interactions
            for peer in interaction.structures]


def sorted_report_findings(findings: list[ReportFinding]) -> list[ReportFinding]:
    """Apply the declared deterministic scientific total ordering."""
    return sorted(findings, key=lambda f: (
        *_peer_total_order_key(f.structure), f.kind, f.label, f.rank))


def filter_report_findings(findings: list[ReportFinding],
                           severity: str = "All",
                           site: str = "All",
                           kind: str = "All",
                           ) -> list[ReportFinding]:
    """Filter normalized findings for the Problems-first view."""
    severity_norm = severity.lower()
    site_norm = site.lower()
    kind_norm = kind.lower()

    def keep(f: ReportFinding) -> bool:
        if severity_norm == "flagged" and f.severity not in {"problem", "caution"}:
            return False
        if severity_norm not in {"all", "flagged"} and f.severity != severity_norm:
            return False
        if site_norm == "3' end" and not f.involves_3prime:
            return False
        if site_norm == "internal" and (not f.found or f.involves_3prime):
            return False
        if kind_norm != "all" and f.kind.lower() != kind_norm:
            return False
        return True

    return [f for f in findings if keep(f)]


def hetero_dimer_matrix(oligos: list[Oligo],
                        report: AnalysisReport
                        ) -> dict[tuple[str, str], HeteroMatrixCell]:
    """Return strongest dimer findings for oligo-name pairs.

    Off-diagonal cells are hetero-dimers; diagonal cells are self-dimers.
    """
    known = {o.name for o in oligos}
    cells: dict[tuple[str, str], HeteroMatrixCell] = {}
    for interaction in report.self_dimers:
        if interaction.label not in known or not interaction.structures:
            continue
        best = _finding_from_structure(
            interaction.structures[0], "self_dimers")
        cells[(interaction.label, interaction.label)] = HeteroMatrixCell(
            interaction.label, interaction.label, best)
    for interaction in report.hetero_dimers:
        if " x " not in interaction.label or not interaction.structures:
            continue
        left, right = interaction.label.split(" x ", 1)
        if left not in known or right not in known:
            continue
        best = _finding_from_structure(
            interaction.structures[0], "hetero_dimers")
        cells[(left, right)] = HeteroMatrixCell(left, right, best)
        cells[(right, left)] = HeteroMatrixCell(right, left, best)
    return cells


def build_oligos(fwd: str = "", rev: str = "",
                 probe1: str = "", probe2: str = "",
                 cond: Optional[ReactionConditions] = None) -> list[Oligo]:
    """Validate raw inputs and build the list of Oligo objects (expanding any
    IUPAC degeneracy).

    All fields are optional but at least ONE sequence must be given. With a
    single oligo only its hairpins and self-dimers are reported (no hetero-
    dimers). Raises SequenceError (message prefixed with the field name) on
    invalid input, or if every field is empty.
    """
    cond = cond or ReactionConditions()
    specs = [
        ("Forward primer", fwd, "primer", cond.primer_conc),
        ("Reverse primer", rev, "primer", cond.primer_conc),
        ("Probe 1", probe1, "probe", cond.probe_conc),
        ("Probe 2", probe2, "probe", cond.probe_conc),
    ]
    oligos: list[Oligo] = []
    for name, raw, role, conc in specs:
        raw = (raw or "").strip()
        if not raw:
            continue
        try:
            seq = clean_sequence(raw)
            variants = expand_iupac(seq)
        except SequenceError as e:
            raise SequenceError(f"{name}: {e}") from e
        oligos.append(Oligo(name=name, seq=seq, role=role, conc_nM=conc,
                            variants=variants))
    if not oligos:
        raise SequenceError("Enter at least one oligo sequence.")
    return oligos


def _looks_like_seq(tok: str) -> bool:
    t = tok.strip().upper().replace("U", "T")
    return len(t) >= 5 and all(c in _IUPAC for c in t)


def parse_bulk_oligos(text: str) -> list[tuple[Optional[str], str]]:
    """Parse a block where each NON-EMPTY LINE is one oligo. A line may be a
    bare sequence or 'label <sep> SEQUENCE' (sep = space/tab/,/;/:/=). Returns
    a list of (label_or_None, sequence) in input order; non-sequence lines are
    skipped."""
    items: list[tuple[Optional[str], str]] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        seq = next((t for t in re.split(r"[\s,;:=]+", s) if _looks_like_seq(t)),
                   None)
        if seq is None:
            if _looks_like_seq(s):
                seq = s
            else:
                continue
        label = s[:s.find(seq)].strip(" \t,;:=>-") or None
        items.append((label, seq))
    return items


def label_uses_probe_concentration(label: Optional[str]) -> bool:
    """Match case-insensitive alphanumeric label tokens for probe concentration.

    A token must be p/pr with optional digits, or contain "probe". Thus p1
    selects probe concentration, while primer retains primer concentration."""
    if not label:
        return False
    tokens = [t for t in re.split(r"[^a-z0-9]+", label.lower()) if t]
    return any(re.fullmatch(r"(?:p|pr)\d*", t) or "probe" in t
               for t in tokens)


def build_oligo_list(items: list[tuple[Optional[str], str]],
                     cond: Optional[ReactionConditions] = None) -> list[Oligo]:
    """Build Oligo objects from a list of (label, raw_sequence) pairs (e.g. a
    multiplex panel pasted one oligo per line). Unlabelled oligos are named
    'Oligo N'; label_uses_probe_concentration selects role and concentration.

    Raises SequenceError (prefixed with the oligo name) on invalid input."""
    cond = cond or ReactionConditions()
    oligos: list[Oligo] = []
    for i, (label, raw) in enumerate(items, 1):
        name = label or f"Oligo {i}"
        is_probe = label_uses_probe_concentration(label)
        role = "probe" if is_probe else "primer"
        conc = cond.probe_conc if is_probe else cond.primer_conc
        try:
            seq = clean_sequence(raw)
            variants = expand_iupac(seq)
        except SequenceError as e:
            raise SequenceError(f"{name}: {e}") from e
        oligos.append(Oligo(name=name, seq=seq, role=role, conc_nM=conc,
                            variants=variants))
    return oligos


def analysis_step_count(oligos: list[Oligo]) -> int:
    return 3 * len(oligos) + (len(oligos) * (len(oligos) - 1) // 2)


def _raise_if_cancelled(cancel_event: Optional[CancelEvent]) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise AnalysisCancelled("Analysis cancelled.")


def _emit_progress(callback: Optional[ProgressCallback],
                   completed: int, total: int, message: str) -> None:
    if callback is not None:
        callback(AnalysisProgress(completed, total, message))


class _CoreResultCache:
    """Bounded exact cache of detached core Primer3/Vienna results."""

    def __init__(self, maxsize: int = 4096) -> None:
        self._maxsize = maxsize
        self._values: OrderedDict[tuple[object, ...], object] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple[object, ...]):
        with self._lock:
            value = self._values.get(key)
            if value is None:
                return None
            self._values.move_to_end(key)
            return copy.deepcopy(value)

    def put(self, key: tuple[object, ...], value: object) -> None:
        with self._lock:
            self._values[key] = copy.deepcopy(value)
            self._values.move_to_end(key)
            while len(self._values) > self._maxsize:
                self._values.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._values.clear()


_CORE_RESULT_CACHE = _CoreResultCache()


@dataclass(frozen=True)
class _CachedCoreResult:
    value: object
    outcomes: tuple[
        tuple[str, str, str, int, tuple[object, ...] | None], ...]
    primer3_records: tuple[tuple[tuple[object, ...], tuple], ...] = ()


def clear_analysis_result_cache() -> None:
    """Clear orchestration core results and external-adapter result caches.

    Vienna caches and native parameter tables remain warm. Use a fresh process
    when profiling a cold start."""

    _CORE_RESULT_CACHE.clear()
    ee.clear_external_result_cache()


def _conditions_cache_key(cond: ReactionConditions) -> tuple[object, ...]:
    return tuple(sorted((name, getattr(cond, name)) for name in (
        "mv_conc", "dv_conc", "dntp_conc", "primer_conc", "probe_conc",
        "dg_temp_c", "dg_caution", "dg_problem",
        "near_duplicate_bond_difference", "dimer_max_consecutive_gaps",
        "dimer_max_total_gaps")))


def _oligo_cache_key(oligo: Oligo) -> tuple[object, ...]:
    return (oligo.name, oligo.seq, oligo.role, float(oligo.conc_nM),
            tuple(oligo.variants))


def _cached_core_result(kind: str, participants: tuple[Oligo, ...],
                        cond: ReactionConditions, compute, *,
                        coverage_scope: tuple[object, ...] = (),
                        primer3_record_limit: int = 0):
    implementation = {
        "tm": calc_tm,
        "hairpin": analyze_hairpin,
        "self-dimer": analyze_self_dimer,
        "hetero-dimer": analyze_hetero,
    }[kind]
    key = (
        "core_result_v2", kind, id(implementation),
        str(getattr(primer3, "__version__", "")),
        str(getattr(primer3, "__primer3_version__", "")), vb.version(),
        coverage_scope,
        primer3_record_limit,
        _conditions_cache_key(cond),
        tuple(_oligo_cache_key(oligo) for oligo in participants),
    )
    cached = _CORE_RESULT_CACHE.get(key)
    if cached is not None:
        if not isinstance(cached, _CachedCoreResult):
            raise RuntimeError("unexpected legacy core cache payload")
        for engine, outcome_kind, status, count, context_id in cached.outcomes:
            _record_coverage_outcome(
                engine, outcome_kind, status, count, context_id=context_id)
        records = _ACTIVE_PRIMER3_RECORDS.get()
        if records is not None:
            records.update(cached.primer3_records)
        return cached.value
    diagnostics = _diagnostics()
    before = len(diagnostics) if diagnostics is not None else 0
    coverage_outcomes = _ACTIVE_COVERAGE_OUTCOMES.get()
    outcome_before = (len(coverage_outcomes)
                      if coverage_outcomes is not None else 0)
    run_records = _ACTIVE_PRIMER3_RECORDS.get()
    local_records: dict[tuple[object, ...], tuple] = {}
    records_token = _ACTIVE_PRIMER3_RECORDS.set(local_records)
    limit_token = _ACTIVE_PRIMER3_RECORD_LIMIT.set(primer3_record_limit)
    try:
        value = compute()
    finally:
        _ACTIVE_PRIMER3_RECORD_LIMIT.reset(limit_token)
        _ACTIVE_PRIMER3_RECORDS.reset(records_token)
    if run_records is not None:
        run_records.update(local_records)
    # Never preserve a calculation that produced a backend diagnostic.  A
    # later valid runtime must get a fresh chance instead of replaying failure.
    if diagnostics is None or len(diagnostics) == before:
        new_outcomes = (() if coverage_outcomes is None else tuple(
            (
                str(item["engine"]), str(item["kind"]),
                str(item["status"]), int(item.get("count", 1)),
                (None if item.get("context_id") is None
                 else tuple(item["context_id"])),
            )
            for item in coverage_outcomes[outcome_before:]))
        _CORE_RESULT_CACHE.put(
            key, _CachedCoreResult(value, new_outcomes, tuple(local_records.items())))
    return value


@dataclass(frozen=True)
class _ExternalTask:
    result: InteractionResult
    sequence_a: str
    sequence_b: str | None
    conditions: ReactionConditions
    self_dimer: bool
    message: str
    target_kind: str
    target_index: int
    context_id: tuple[object, ...] | None = None
    baseline: bool = True


@dataclass(frozen=True)
class _FinalizedExternalTask:
    result: InteractionResult
    diagnostics: tuple[sm.ScientificDiagnostic, ...]
    coverage_outcomes: tuple[dict[str, object], ...] = ()


def _build_external_tasks(
    oligos: list[Oligo], report: AnalysisReport, cond: ReactionConditions,
) -> list[_ExternalTask]:
    tasks: list[_ExternalTask] = []
    for index, (oligo, result) in enumerate(zip(oligos, report.hairpins)):
        sequence = result.representative_variant or oligo.variants[0]
        variant_index = oligo.variants.index(sequence)
        tasks.append(_ExternalTask(
            result, sequence, None, cond.for_conc(oligo.conc_nM), False,
            f"Hairpin: {oligo.name}", "hairpin", index,
            ("hairpin", index, variant_index, None, None)))
    for index, (oligo, result) in enumerate(zip(oligos, report.self_dimers)):
        sequence = result.representative_variant or oligo.variants[0]
        variant_index = oligo.variants.index(sequence)
        tasks.append(_ExternalTask(
            result, sequence, sequence, cond.for_conc(oligo.conc_nM), True,
            f"Self-dimer: {oligo.name}", "self_dimer", index,
            ("self_dimer", index, variant_index, index, variant_index)))
    oligo_pairs = list(combinations(range(len(oligos)), 2))
    for index, ((left_index, right_index), result) in enumerate(zip(
            oligo_pairs, report.hetero_dimers)):
        a, b = oligos[left_index], oligos[right_index]
        if (result.representative_variant
                and " / " in result.representative_variant):
            sequence_a, sequence_b = result.representative_variant.split(" / ", 1)
        else:
            sequence_a, sequence_b = a.variants[0], b.variants[0]
        tasks.append(_ExternalTask(
            result, sequence_a, sequence_b,
            cond.for_conc(max(a.conc_nM, b.conc_nM)), False,
            f"Hetero-dimer: {a.name} x {b.name}",
            "hetero_dimer", index,
            ("hetero_dimer", left_index, a.variants.index(sequence_a),
             right_index, b.variants.index(sequence_b))))
    return tasks


_AdditionalSequenceContext = tuple[
    str, int, int, int | None, str, str | None]


def _iter_additional_hairpin_contexts(
    oligos: list[Oligo], report: AnalysisReport,
) -> Iterator[_AdditionalSequenceContext]:
    for index, (oligo, result) in enumerate(zip(oligos, report.hairpins)):
        representative = result.sequence_a
        for sequence in oligo.variants:
            if sequence != representative:
                yield "hairpin", index, index, None, sequence, None


def _iter_additional_self_dimer_contexts(
    oligos: list[Oligo], report: AnalysisReport,
) -> Iterator[_AdditionalSequenceContext]:
    for index, (oligo, result) in enumerate(zip(oligos, report.self_dimers)):
        representative = result.sequence_a
        for sequence in oligo.variants:
            if sequence != representative:
                yield "self_dimer", index, index, None, sequence, sequence


def _iter_additional_hetero_dimer_contexts(
    oligos: list[Oligo], report: AnalysisReport,
) -> Iterator[_AdditionalSequenceContext]:
    index = 0
    for left_index in range(len(oligos)):
        for right_index in range(left_index + 1, len(oligos)):
            left, right = oligos[left_index], oligos[right_index]
            result = report.hetero_dimers[index]
            representative = (result.sequence_a, result.sequence_b)
            for sequence_a in left.variants:
                for sequence_b in right.variants:
                    if (sequence_a, sequence_b) != representative:
                        yield (
                            "hetero_dimer", index, left_index, right_index,
                            sequence_a, sequence_b)
            index += 1


def _iter_additional_sequence_contexts(
    oligos: list[Oligo], report: AnalysisReport, plan: EnsembleWorkPlan,
) -> Iterator[_AdditionalSequenceContext]:
    """Resolve bounded additional slots around Primer3's actual representatives."""

    baseline_hairpins = len(oligos)
    baseline_self_dimers = len(oligos)
    baseline_hetero_dimers = len(oligos) * (len(oligos) - 1) // 2
    yield from islice(
        _iter_additional_hairpin_contexts(oligos, report),
        plan.kind("hairpin").allocated - baseline_hairpins)
    yield from islice(
        _iter_additional_self_dimer_contexts(oligos, report),
        plan.kind("self_dimer").allocated - baseline_self_dimers)
    yield from islice(
        _iter_additional_hetero_dimer_contexts(oligos, report),
        plan.kind("hetero_dimer").allocated - baseline_hetero_dimers)


def iter_resolved_ensemble_contexts(
    oligos: list[Oligo], report: AnalysisReport, plan: EnsembleWorkPlan,
) -> Iterator[EnsembleContext]:
    """Yield allocated contexts bound to deterministic representatives."""

    for index, (oligo, result) in enumerate(zip(oligos, report.hairpins)):
        sequence = (result.sequence_a if result.sequence_a in oligo.variants
                    else oligo.variants[0])
        variant = oligo.variants.index(sequence)
        yield EnsembleContext(
            "hairpin", index, variant, sequence, baseline=True)
    for index, (oligo, result) in enumerate(zip(oligos, report.self_dimers)):
        sequence = (result.sequence_a if result.sequence_a in oligo.variants
                    else oligo.variants[0])
        variant = oligo.variants.index(sequence)
        yield EnsembleContext(
            "self_dimer", index, variant, sequence,
            index, variant, sequence, True)
    for (left, right), result in zip(
            combinations(range(len(oligos)), 2), report.hetero_dimers):
        sequence_a = (result.sequence_a
                      if result.sequence_a in oligos[left].variants
                      else oligos[left].variants[0])
        sequence_b = (result.sequence_b
                      if result.sequence_b in oligos[right].variants
                      else oligos[right].variants[0])
        variant_a = oligos[left].variants.index(sequence_a)
        variant_b = oligos[right].variants.index(sequence_b)
        yield EnsembleContext(
            "hetero_dimer", left, variant_a, sequence_a,
            right, variant_b, sequence_b, True)
    for (kind, target, left_index, right_index, sequence_a,
         sequence_b) in _iter_additional_sequence_contexts(
            oligos, report, plan):
        if kind == "hairpin":
            yield EnsembleContext(
                kind, target, oligos[target].variants.index(sequence_a),
                sequence_a)
        elif kind == "self_dimer":
            variant = oligos[target].variants.index(sequence_a)
            yield EnsembleContext(
                kind, target, variant, sequence_a,
                target, variant, sequence_a)
        else:
            left, right = left_index, right_index
            assert right is not None
            assert sequence_b is not None
            yield EnsembleContext(
                kind, left, oligos[left].variants.index(sequence_a),
                sequence_a, right,
                oligos[right].variants.index(sequence_b), sequence_b)


def _resolve_ensemble_plan_contexts(
    oligos: list[Oligo], report: AnalysisReport, plan: EnsembleWorkPlan,
) -> EnsembleWorkPlan:
    """Bind allocated slots to Primer3's deterministic representatives."""

    contexts = tuple(iter_resolved_ensemble_contexts(oligos, report, plan))
    if len(contexts) != plan.allocated_contexts:
        raise RuntimeError("resolved ensemble schedule does not match allocation")
    return replace(plan, contexts=contexts)


def _additional_context_core_result(
    *, kind: str, label: str, sequence_a: str,
    sequence_b: str | None, n_combos: int, cond: ReactionConditions,
    cancel_event: CancelEvent | None,
    context_id: tuple[object, ...],
) -> InteractionResult:
    """Retain Primer3 and discover local/Vienna peers for an allocated context."""

    peer_kind = {
        "hairpin": "Hairpin",
        "self_dimer": "Self-dimer",
        "hetero_dimer": "Hetero-dimer",
    }[kind]
    representative = (sequence_a if sequence_b is None
                      else f"{sequence_a} / {sequence_b}")
    candidates: list[PeerStructure] = []
    _raise_if_cancelled(cancel_event)
    record = (_ACTIVE_PRIMER3_RECORDS.get() or {}).get(context_id)
    if record is not None:
        found, dg, tm, structure, variant = record
        peer = _primer3_peer(
            peer_kind, label, found, dg, tm, structure, sequence_a, sequence_b,
            n_combos, variant)
        if peer is not None:
            candidates.append(peer)
    if kind == "hairpin":
        local_items = _observed_discovery(
            "LocalEnumerator", kind,
            lambda: nn.enumerate_hairpins(
                sequence_a, cond, LOCAL_ENUMERATION_POOL),
            context_id=context_id)
    else:
        assert sequence_b is not None
        local_items = _observed_discovery(
            "LocalEnumerator", kind,
            lambda: nn.enumerate_duplex(
                sequence_a, sequence_b, cond, LOCAL_ENUMERATION_POOL,
                self_dimer=kind == "self_dimer"),
            context_id=context_id)
    candidates.extend(peer for item in local_items
                      if (peer := _peer_from_local(
                          item, label, n_combos, representative)) is not None)
    _raise_if_cancelled(cancel_event)
    if kind == "hairpin":
        vienna_items = _observed_discovery(
            "ViennaRNA", kind,
            lambda: vb.discover_hairpin_structures(
                sequence_a, cond, VIENNA_MAX_STRUCTURES, _diagnostics()),
            context_id=context_id)
    else:
        assert sequence_b is not None
        vienna_items = _observed_discovery(
            "ViennaRNA", kind,
            lambda: vb.discover_dimer_structures(
                sequence_a, sequence_b, cond, _diagnostics()),
            context_id=context_id)
    candidates.extend(peer for item in vienna_items
                      if (peer := _peer_from_vienna(
                          item, peer_kind, label, sequence_a, sequence_b,
                          n_combos, representative)) is not None)
    result = InteractionResult(
        peer_kind, label, sequence_a, sequence_b,
        n_combos=n_combos, representative_variant=representative,
        variant_coverage="budgeted_concrete_context")
    result.structures = _finalize_peer_structures(
        peer_kind, label, sequence_a, sequence_b, candidates, cond,
        score_fixed_metrics=True,
        exclude_all_positive_dg=False,
        suppress_near_duplicates=False,
        apply_gap_derivation=False,
        cancel_event=cancel_event)
    return result


def _build_additional_external_tasks(
    oligos: list[Oligo], report: AnalysisReport, cond: ReactionConditions,
    plan: EnsembleWorkPlan, cancel_event: CancelEvent | None,
) -> list[_ExternalTask]:
    tasks: list[_ExternalTask] = []
    for (kind, index, left_index, right_index, sequence_a,
         sequence_b) in _iter_additional_sequence_contexts(
            oligos, report, plan):
        _raise_if_cancelled(cancel_event)
        if kind == "hairpin":
            oligo = oligos[index]
            label = oligo.name
            n_combos = oligo.n_variants
            task_cond = cond.for_conc(oligo.conc_nM)
            message = f"Hairpin: {oligo.name}"
            context_id = (
                kind, index, oligo.variants.index(sequence_a), None, None)
        elif kind == "self_dimer":
            oligo = oligos[index]
            label = oligo.name
            n_combos = oligo.n_variants
            task_cond = cond.for_conc(oligo.conc_nM)
            message = f"Self-dimer: {oligo.name}"
            variant_index = oligo.variants.index(sequence_a)
            context_id = (
                kind, index, variant_index, index, variant_index)
        else:
            assert right_index is not None
            left, right = oligos[left_index], oligos[right_index]
            assert sequence_b is not None
            label = f"{left.name} x {right.name}"
            n_combos = left.n_variants * right.n_variants
            task_cond = cond.for_conc(max(left.conc_nM, right.conc_nM))
            message = f"Hetero-dimer: {label}"
            context_id = (
                kind, left_index, left.variants.index(sequence_a),
                right_index, right.variants.index(sequence_b))
        result = _additional_context_core_result(
            kind=kind, label=label, sequence_a=sequence_a,
            sequence_b=sequence_b, n_combos=n_combos, cond=task_cond,
            cancel_event=cancel_event, context_id=context_id)
        tasks.append(_ExternalTask(
            result, sequence_a, sequence_b, task_cond,
            kind == "self_dimer", message, kind, index, context_id, False))
    return tasks


def _external_task_batch(
    task: _ExternalTask, session: ee.RNAstructureSession,
    cancel_event: Optional[CancelEvent],
) -> ee.EngineBatch:
    _raise_if_cancelled(cancel_event)
    if task.sequence_b is None:
        return _safe_external_batch(lambda: ee.run_hairpin_engines(
            task.sequence_a, task.conditions, session=session,
            cancel_event=cancel_event, rnastructure_only=True))
    return _safe_external_batch(lambda: ee.run_dimer_engines(
        task.sequence_a, task.sequence_b, task.conditions,
        self_dimer=task.self_dimer,
        session=session, cancel_event=cancel_event))


def _finalize_external_task(
    task: _ExternalTask, batch: ee.EngineBatch,
    session: ee.RNAstructureSession,
    cancel_event: Optional[CancelEvent],
) -> _FinalizedExternalTask:
    """Finalize one detached interaction with task-local diagnostics."""

    local_diagnostics: list[sm.ScientificDiagnostic] = []
    token = _ACTIVE_DIAGNOSTICS.set(local_diagnostics)
    try:
        _raise_if_cancelled(cancel_event)
        detached = copy.deepcopy(task.result)
        coverage_outcomes = [
            _batch_coverage_outcome(
                batch, "RNAstructure", task.target_kind,
                context_id=task.context_id)]
        if task.sequence_b is None:
            seqfold_batch = _safe_external_batch(
                lambda: ee.run_seqfold_hairpin_engine(
                    task.sequence_a, task.conditions))
            coverage_outcomes.append(_batch_coverage_outcome(
                seqfold_batch, "seqfold", task.target_kind,
                context_id=task.context_id))
            batch = ee._combine((batch, seqfold_batch))
        result = _merge_external_observations(
            detached, batch, task.conditions,
            rnastructure_session=session,
            cancel_event=cancel_event)
        _raise_if_cancelled(cancel_event)
        return _FinalizedExternalTask(
            result, tuple(local_diagnostics), tuple(coverage_outcomes))
    finally:
        _ACTIVE_DIAGNOSTICS.reset(token)


def _finalize_external_tasks(
    tasks: list[_ExternalTask], batches: list[ee.EngineBatch],
    session: ee.RNAstructureSession,
    cancel_event: Optional[CancelEvent], workers: int,
) -> list[_FinalizedExternalTask]:
    """Stage every final result before the caller publishes any of them."""

    staged: list[_FinalizedExternalTask | None] = [None] * len(tasks)
    dimer_indices: list[int] = []
    for index, (task, batch) in enumerate(zip(tasks, batches)):
        if task.sequence_b is None:
            staged[index] = _finalize_external_task(
                task, batch, session, cancel_event)
        else:
            dimer_indices.append(index)

    if workers == 1 or len(dimer_indices) <= 1:
        for index in dimer_indices:
            staged[index] = _finalize_external_task(
                tasks[index], batches[index], session, cancel_event)
    else:
        dimer_work = list(zip(
            (tasks[index] for index in dimer_indices),
            (batches[index] for index in dimer_indices)))
        finalized_dimers = _bounded_parallel_map(
            dimer_work,
            lambda item: _finalize_external_task(
                item[0], item[1], session, cancel_event),
            workers=workers,
            thread_name_prefix="dimer-finalization",
            cancel_event=cancel_event)
        for index, result in zip(dimer_indices, finalized_dimers):
            staged[index] = result

    if any(item is None for item in staged):
        raise RuntimeError("external finalization produced an incomplete task set")
    return [item for item in staged if item is not None]


def _external_worker_count(requested: int | None) -> int:
    # Bound concurrent native searches to control per-search memory pressure;
    # eight is an operational ceiling, not a scientific parameter. Results are
    # still consumed in task order, so concurrency cannot change report order.
    raw = requested
    if raw is None:
        try:
            raw = int(os.environ.get("MBUPRIME_RNASTRUCTURE_WORKERS", "8"))
        except ValueError:
            raw = 8
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise SequenceError("max_external_workers must be an integer")
    return max(1, min(8, raw))


def _bounded_parallel_map(
    items: list[object], worker: Callable[[object], object], *,
    workers: int, thread_name_prefix: str,
    cancel_event: CancelEvent | None,
) -> list[object]:
    """Evaluate in bounded windows and return results in input order."""

    if workers == 1 or len(items) <= 1:
        return [worker(item) for item in items]
    results: list[object | None] = [None] * len(items)
    executor = ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix=thread_name_prefix)
    inflight: dict[Future[object], int] = {}
    next_index = 0

    def fill_window() -> None:
        nonlocal next_index
        while next_index < len(items) and len(inflight) < workers:
            index = next_index
            next_index += 1
            inflight[executor.submit(worker, items[index])] = index

    try:
        fill_window()
        while inflight:
            _raise_if_cancelled(cancel_event)
            done, _pending = wait(
                tuple(inflight), return_when=FIRST_COMPLETED)
            for future in sorted(done, key=lambda item: inflight[item]):
                index = inflight.pop(future)
                results[index] = future.result()
            fill_window()
    except BaseException:
        for future in inflight:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)
    if any(item is None for item in results):
        raise RuntimeError("bounded executor produced an incomplete result set")
    return [item for item in results if item is not None]


def _report_interactions_for_kind(
    report: AnalysisReport, kind: str,
) -> list[InteractionResult]:
    return {
        "hairpin": report.hairpins,
        "self_dimer": report.self_dimers,
        "hetero_dimer": report.hetero_dimers,
    }[kind]


def _publish_finalized_ensemble_tasks(
    report: AnalysisReport, tasks: list[_ExternalTask],
    finalized: list[_FinalizedExternalTask], cond: ReactionConditions,
    plan: EnsembleWorkPlan,
) -> None:
    """Atomically merge staged concrete contexts into report interactions."""

    for task, item in zip(tasks, finalized):
        interactions = _report_interactions_for_kind(report, task.target_kind)
        target = interactions[task.target_index]
        if task.baseline:
            interactions[task.target_index] = item.result
        else:
            target.structures.extend(item.result.structures)
            target.external_diagnostics = tuple(dict.fromkeys(
                target.external_diagnostics + item.result.external_diagnostics))

    if plan.mode != ENSEMBLE_MODE_BUDGETED_COMPLETE:
        return
    coverage_label = (
        "complete_concrete_ensemble"
        if plan.allocated_contexts == plan.total_contexts
        else "budgeted_partial_concrete_ensemble")
    for interaction in report.all_interactions:
        for peer in interaction.structures:
            # Coverage is reported once at report/manifest level.  Avoid the
            # representative-only warning on fully evaluated concrete peers.
            peer.n_combos = 1
            _classify_peer(peer, cond)
        interaction.variant_coverage = coverage_label
        interaction.structures = _rank_peer_structures(
            interaction.structures, interaction.kind, interaction.label,
            cond=cond, exclude_all_positive_dg=False,
            suppress_near_duplicates=True,
            near_duplicate_scope="logical_interaction")


def _manifest_conditions(cond: ReactionConditions) -> dict[str, object]:
    salt = nn.derive_salt(cond)
    return {
        "mv_conc_mM": float(cond.mv_conc),
        "dv_conc_mM": float(cond.dv_conc),
        "dntp_conc_mM": float(cond.dntp_conc),
        "primer_conc_nM": float(cond.primer_conc),
        "probe_conc_nM": float(cond.probe_conc),
        "dg_temp_c": float(cond.dg_temp_c),
        "dg_caution_kcal_mol": float(cond.dg_caution),
        "dg_problem_kcal_mol": float(cond.dg_problem),
        "near_duplicate_bond_difference": cond.near_duplicate_bond_difference,
        "dimer_max_consecutive_gaps": cond.dimer_max_consecutive_gaps,
        "dimer_max_total_gaps": cond.dimer_max_total_gaps,
        "assumed_free_mg_mM": salt.assumed_free_mg_mM,
        "effective_na_equivalent_M": salt.effective_na_equivalent_M,
        "salt_formula_id": salt.formula_id,
        "salt_clamping_applied": salt.clamping_applied,
    }


def _manifest_integrity_status(status: str, *required_hashes: object) -> str:
    if status != "available":
        return "unavailable"
    return "verified" if all(required_hashes) else "unverified"


def _primer3_manifest_metadata(primer3_identity: Mapping[str, str]) -> dict[str, object]:
    """Return fresh Primer3 capability and validated native-identity metadata."""
    return {
        "status": "available",
        "version": primer3_identity["primer3_py_version"],
        "core_version": primer3_identity["libprimer3_version"],
        "mandatory": True,
        "integration": "in_process_python_extension",
        "hairpin": True,
        "dimer": True,
        "multiple_structures": False,
        "structure_tm_reported": True,
        "severity_eligible": True,
        "target": primer3_identity["target"],
        "thermoanalysis_filename": primer3_identity[
            "thermoanalysis_filename"],
        "thermoanalysis_sha256": primer3_identity[
            "thermoanalysis_sha256"],
        "p3helpers_filename": primer3_identity["p3helpers_filename"],
        "p3helpers_sha256": primer3_identity["p3helpers_sha256"],
        "integrity_status": _manifest_integrity_status(
            "available", primer3_identity["thermoanalysis_sha256"],
            primer3_identity["p3helpers_sha256"]),
    }


def create_scientific_manifest(
    oligos: list[Oligo], cond: ReactionConditions,
    diagnostics: tuple[sm.ScientificDiagnostic, ...] = (),
    rnastructure_session: ee.RNAstructureSession | None = None,
    *,
    ensemble_plan: EnsembleWorkPlan | None = None,
    ensemble_coverage: EnsembleCoverage | None = None,
    primer3_identity: Mapping[str, str] | None = None,
    vienna_identity: Mapping[str, str] | None = None,
    vienna_version: str | None = None,
) -> sm.ScientificManifest:
    """Build the reproducibility manifest from normalized inputs and live engines.

    Engine capability, integrity, search, severity, geometry, and ensemble
    policies are recorded as evidence; this function does not reinterpret the
    completed scientific results.
    """

    if rnastructure_session is None:
        with ee.RNAstructureSession() as session:
            return create_scientific_manifest(
                oligos, cond, diagnostics, rnastructure_session=session,
                ensemble_plan=ensemble_plan,
                ensemble_coverage=ensemble_coverage,
                primer3_identity=primer3_identity,
                vienna_identity=vienna_identity,
                vienna_version=vienna_version)
    if primer3_identity is None:
        primer3_identity = sm.validated_primer3_runtime_identity(primer3)
    if vienna_identity is None:
        vienna_identity = sm.validated_vienna_runtime_identity()
    if vienna_version is None:
        vienna_version = vb.require_operational(cond)
    # The loaded-module identity above is authoritative. ``require_operational``
    # additionally proves the configured salt/parameter API and is retained as
    # a separate capability gate.
    vienna_version = vienna_identity["version"]
    if ensemble_plan is None:
        ensemble_plan = plan_ensemble_work(oligos)
    if ensemble_coverage is None:
        # A manifest created without an AnalysisReport has no observed engine
        # outcomes.  Preserve that absence instead of claiming peer success.
        ensemble_coverage = build_ensemble_coverage(ensemble_plan, ())
    normalized_inputs = [
        {
            "input_order": index,
            "name": oligo.name,
            "sequence": oligo.seq,
            "role": oligo.role,
            "concentration_nM": float(oligo.conc_nM),
            "variant_count": oligo.n_variants,
        }
        for index, oligo in enumerate(oligos, 1)
    ]
    capabilities = ee.discover_external_engines(rnastructure_session)
    engine_metadata: dict[str, object] = {
        "Primer3": _primer3_manifest_metadata(primer3_identity),
        "ViennaRNA": {
            "status": "available" if vb.available() else "unavailable",
            "version": vienna_version,
            "mandatory": True,
            "integration": "in_process_python_binding",
            "hairpin": True,
            "dimer": True,
            "multiple_structures": True,
            "structure_tm_reported": True,
            "severity_eligible": True,
            "target": vienna_identity["target"],
            "native_binding_filename": vienna_identity[
                "native_binding_filename"],
            "native_binding_sha256": vienna_identity[
                "native_binding_sha256"],
            "integrity_status": _manifest_integrity_status(
                "available", vienna_identity["native_binding_sha256"]),
        },
    }
    for engine in ee.ENGINE_ORDER:
        capability = capabilities[engine]
        artifact_hash = ""
        engine_metadata[engine] = {
            "status": capability.status,
            "version": capability.version,
            "mandatory": True,
            "artifact_sha256": artifact_hash,
            "hairpin": capability.hairpin,
            "dimer": capability.dimer,
            "multiple_structures": capability.multiple_structures,
            "structure_tm_reported": capability.structure_tm,
            "severity_eligible": True,
        }
        if engine == "RNAstructure":
            native_identity = dict(getattr(
                rnastructure_session, "native_identity", {}) or {})
            engine_metadata[engine].update({
                "target": native_identity.get("target", ""),
                "artifact_sha256": native_identity.get(
                    "native_module_sha256", ""),
                "integration": native_identity.get(
                    "integration", "in_process_native"),
                "abi_tag": native_identity.get("abi_tag", ""),
                "native_module_sha256": native_identity.get(
                    "native_module_sha256", ""),
                "upstream_source_archive_sha256": native_identity.get(
                    "upstream_source_archive_sha256", ""),
                "source_manifest_sha256": native_identity.get(
                    "source_manifest_sha256", ""),
                "dna_table_manifest_sha256": native_identity.get(
                    "dna_table_manifest_sha256", ""),
                "compatibility_patch": native_identity.get(
                    "compatibility_patch", ""),
                "integrity_status": _manifest_integrity_status(
                    capability.status,
                    native_identity.get("native_module_sha256"),
                    native_identity.get("source_manifest_sha256"),
                    native_identity.get("dna_table_manifest_sha256")),
                "thermo_runtime_status": capability.status,
                "dg_temperature_adjustable": True,
                "thermo_method": "fixed_geometry_inner_dh_ds",
                "temperature_unit": "kelvin",
                "hairpin_tm_search_range_c": list(
                    ee.RNASTRUCTURE_HAIRPIN_TM_SEARCH_RANGE_C),
                "dimer_tm_search_range_c": list(
                    ee.RNASTRUCTURE_DIMER_TM_SEARCH_RANGE_C),
                "energy_output_resolution_kcal_mol": 0.1,
                "tm_tolerance_policy": (
                    "propagated per structure from 0.1 kcal/mol G37/H bins; "
                    "see observation bracket and numerical_tolerance_c"),
                "hairpin_tm_definition": (
                    "intrinsic fixed-geometry dG=0 crossing"),
                "dimer_tm_definition": (
                    "Primer3-style concentration-adjusted fixed-geometry "
                    "dimer Tm using C_T/4 and salt-corrected entropy"),
                "dimer_tm_formula_id": (
                    ee.RNASTRUCTURE_DIMER_TM_FORMULA_ID),
                "dimer_tm_concentration_divisor": (
                    ee.RNASTRUCTURE_DIMER_TM_CONCENTRATION_DIVISOR),
                "dimer_tm_gas_constant_cal_mol_k": (
                    ee.RNASTRUCTURE_DIMER_TM_GAS_CONSTANT_CAL_MOL_K),
                "dimer_tm_salt_entropy_formula": (
                    ee.RNASTRUCTURE_DIMER_SALT_ENTROPY_FORMULA),
                "dimer_tm_symmetry_policy": (
                    ee.RNASTRUCTURE_DIMER_TM_SYMMETRY_POLICY),
                "dimer_tm_symmetry_caveat": (
                    ee.RNASTRUCTURE_DIMER_TM_SYMMETRY_CAVEAT),
                "dimer_tm_concentration_source": (
                    "self: oligo concentration; heterodimer: maximum of both "
                    "oligo concentrations"),
                "compatibility_patch_policy": (
                    "strict isolated 6.6 tables: migrate legacy terminal-A/T "
                    "enthalpy 3.2 kcal/mol into helix_ends.dh plus remove the "
                    "deprecated miscloop terminal-AU fields"),
            })
        elif engine == "seqfold":
            try:
                seqfold_identity = ee._validated_seqfold_identity()
            except ee.RequiredScientificEngineError:
                seqfold_identity = {}
            source_hash = seqfold_identity.get(
                "python_source_manifest_sha256", "")
            native_core_hash = seqfold_identity.get("native_core_sha256", "")
            engine_metadata[engine].update({
                "target": seqfold_identity.get("target", ""),
                "artifact_sha256": seqfold_identity.get(
                    "artifact_sha256", ""),
                "integration": "in_process_python",
                "integrity_status": _manifest_integrity_status(
                    capability.status, source_hash, native_core_hash),
                "python_source_manifest_sha256": source_hash,
                "native_core_sha256": native_core_hash,
            })
    return sm.build_manifest(
        normalized_inputs=normalized_inputs,
        conditions=_manifest_conditions(cond),
        diagnostics=diagnostics,
        primer3_py_version=primer3_identity["primer3_py_version"],
        primer3_core_version=primer3_identity["libprimer3_version"],
        vienna_version=vienna_version,
        vienna_salt_applied=True,
        max_variants=MAX_VARIANTS,
        enumeration_pool=ENUMERATION_POOL,
        min_sequence_length=MIN_SEQUENCE_LENGTH,
        max_sequence_length=MAX_SEQUENCE_LENGTH,
        engine_search_policies={
            "Primer3": {"limit": 1, "mode": "selected structure per concrete context"},
            "ViennaRNA": {
                "unique_geometry_limit": VIENNA_MAX_STRUCTURES,
                "hairpin_mode": "MFE plus deterministic unique Zuker structures",
                "dimer_limit": 1,
                "dimer_mode": "connected-complex MFE; Zuker cofold unsupported in 2.7.2",
            },
            "RNAstructure": {"maximum": RNASTRUCTURE_MAX_STRUCTURES},
            "seqfold": {"limit": 1, "mode": "hairpin MFE"},
            "LocalEnumerator": {"pool": LOCAL_ENUMERATION_POOL},
        },
        # Every discovered geometry in the allocated contexts enters the
        # union; ensemble omission is reported separately by the coverage
        # ledger and must not overload this historical field.
        final_union_truncated=False,
        degenerate_structure_scope=(
            "complete_concrete_variant_ensemble"
            if ensemble_coverage.ensemble_complete else
            "budgeted_partial_concrete_variant_ensemble"
            if ensemble_plan.mode == ENSEMBLE_MODE_BUDGETED_COMPLETE else
            "primer3_selected_representative_variant"),
        severity_policy={
            "hairpin_dg_engines": ["Primer3", "ViennaRNA", "RNAstructure", "seqfold"],
            "hairpin_tm_engines": ["Primer3", "ViennaRNA", "RNAstructure", "seqfold"],
            "dimer_dg_engines": ["Primer3", "ViennaRNA", "RNAstructure"],
            "dimer_tm_severity": False,
            "seven_pairs_without_tm": "caution",
            "flagged_table_dg": "lowest supported finite dG; bare two-decimal display",
            "flagged_table_tm": "highest supported finite Tm; bare two-decimal display",
            "flagged_table_pair_count": "canonical bonded nucleotide-pair count",
            "gui_result_publication": "complete mandatory-engine reports only",
        },
        geometry_policy={
            "identity": "kind + concrete ordered sequences + pair set; dimers permit strand-swap identity",
            "backend_mismatch_wobble_retained": True,
            "hairpin_noncrossing_required": True,
            "positive_dg_exclusion": {
                "rule": (
                    "omit_if_nonempty_supported_finite_dg_values_are_all_positive"),
                "hairpin_engines": [
                    "Primer3", "ViennaRNA", "RNAstructure", "seqfold"],
                "dimer_engines": [
                    "Primer3", "ViennaRNA", "RNAstructure"],
            },
            "near_duplicate_visibility": {
                "scope": (
                    "concrete context by default; finalized budgeted-complete "
                    "union compares variants only within one logical interaction"),
                "distance": (
                    "symmetric difference of canonical bonded base-pair "
                    "identities"),
                "bond_difference_exclusive_maximum": (
                    cond.near_duplicate_bond_difference),
                "same_site_required": True,
                "same_concrete_sequence_context_required": (
                    "except in finalized budgeted-complete logical-interaction union"),
                "equal_strand_lengths_required": True,
                "site_classes": ["internal", "3_prime_end"],
                "hairpin_metric": {
                    "rule": (
                        "retain higher maximum validated exact-geometry Tm"),
                    "engines": [
                        "Primer3", "ViennaRNA", "RNAstructure", "seqfold"],
                    "decision_resolution_c": str(sm.TM_DECISION_RESOLUTION),
                },
                "dimer_metric": {
                    "rule": (
                        "retain lower minimum supported exact-geometry dG"),
                    "engines": ["Primer3", "ViennaRNA", "RNAstructure"],
                    "decision_resolution_kcal_mol": str(
                        sm.DG_DECISION_RESOLUTION),
                },
                "equal_or_missing_metric": "retain both",
                "logical_interaction_equal_metric": (
                    "retain lower deterministic total-order peer"),
                "missing_metric": "retain both",
                "dominance": (
                    "direct comparison against original post-positive-filter "
                    "visible set; non-transitive and order-independent"),
                "ranking": "contiguous rerank after suppression",
            },
            "dimer_gap_visibility": {
                "maximum_consecutive_internal_gaps": (
                    cond.dimer_max_consecutive_gaps),
                "maximum_total_internal_gaps": cond.dimer_max_total_gaps,
                "leading_trailing_padding_counted": False,
                "policy": DIMER_GAP_DERIVATION_POLICY,
                "derivation": (
                    "replace a failing monotonic parent with a maximum-bond "
                    "subset of complete maximal antiparallel stacked blocks"),
                "minimum_retained_block_pairs": 2,
                "candidate_limit": DIMER_DERIVATION_CANDIDATE_CAP,
                "candidate_limit_behavior": (
                    "fail closed with typed scientific diagnostic; no partial "
                    "candidate winner"),
                "rescoring": (
                    "derived geometry is exactly rescored by ViennaRNA and "
                    "RNAstructure before positive-dG, near-duplicate, and "
                    "ranking policies"),
                "metric_ownership": (
                    "changed geometry clears parent Primer3 metrics and all "
                    "parent exact-geometry observations"),
                "ordering": (
                    "parent exact deduplication; derivation and exact "
                    "rescoring; derived/final exact deduplication; positive-dG "
                    "exclusion; near-duplicate suppression; ranking"),
            },
        },
        variant_policy={
            "primer3": (
                "all concrete variants evaluated; minimum-dG representative "
                "selected; calculated peers retained for every allocated context"),
            "other_engines": (
                "all allocated concrete contexts; hairpins and self-dimers "
                "are allocated before heterodimers"),
            "complete_cross_engine_variant_coverage": (
                ensemble_coverage.ensemble_complete),
            "mode": ensemble_plan.mode,
            "configured_additional_budget": (
                ensemble_plan.configured_additional_budget),
        },
        engines=engine_metadata,
        ensemble_plan=ensemble_plan.to_dict(),
        ensemble_coverage=ensemble_coverage.to_dict(),
    )


def ensure_report_manifest(oligos: list[Oligo], report: AnalysisReport,
                           cond: ReactionConditions) -> sm.ScientificManifest:
    """Return export provenance, attaching a new manifest when it is absent.

    Incomplete reports raise AnalysisIncompleteError before any attachment.
    For complete reports, a missing manifest is built from these inputs and
    assigned to report.manifest; runtime/provenance errors propagate. Existing
    manifests are reused. Missing, unavailable, or unverified RNAstructure or
    seqfold metadata then raises AnalysisIncompleteError, even if a manifest
    was just attached. This export gate does not recompute results or upgrade
    partial ensemble coverage to exhaustive coverage.

    All report formatters share this mutation and rejection contract.
    """
    if not report.complete:
        raise AnalysisIncompleteError(
            f"analysis report phase '{report.phase}' is not exportable")
    if report.manifest is None:
        report.manifest = create_scientific_manifest(
            oligos, cond, report.diagnostics,
            ensemble_plan=report.ensemble_plan,
            ensemble_coverage=report.ensemble_coverage)
    payload = report.manifest.to_dict().get("engines", {})
    for engine in ("RNAstructure", "seqfold"):
        metadata = payload.get(engine, {}) if isinstance(payload, dict) else {}
        if (metadata.get("status") != "available" or
                metadata.get("integrity_status") != "verified"):
            raise AnalysisIncompleteError(
                f"mandatory engine {engine} is unavailable or untrusted")
    return report.manifest


def _filtered_partial_report(
    report: AnalysisReport, cond: ReactionConditions,
) -> AnalysisReport:
    """Return a detached preview with the final visibility rule applied.

    The working report retains every canonical core peer so a later engine
    observation can merge into that same geometry without losing Primer3 or
    ViennaRNA metrics and discovery provenance.
    """

    preview = copy.deepcopy(report)
    preview_diagnostics = list(preview.diagnostics)
    token = _ACTIVE_DIAGNOSTICS.set(preview_diagnostics)
    try:
        for interaction in preview.all_interactions:
            interaction.structures = _finalize_peer_structures(
                interaction.kind, interaction.label,
                interaction.sequence_a, interaction.sequence_b,
                interaction.structures, cond,
                score_fixed_metrics=True,
                exclude_all_positive_dg=True,
                suppress_near_duplicates=False)
        preview.diagnostics = tuple(preview_diagnostics)
    finally:
        _ACTIVE_DIAGNOSTICS.reset(token)
    return preview


def _run_core_analysis_phase(
    oligos: list[Oligo], cond: ReactionConditions, report: AnalysisReport,
    ensemble_plan: EnsembleWorkPlan,
    progress_callback: Optional[ProgressCallback],
    cancel_event: Optional[CancelEvent],
    partial_callback: Optional[PartialReportCallback],
    diagnostics: list[sm.ScientificDiagnostic],
    total: int,
) -> tuple[EnsembleWorkPlan, int]:
    """Populate Primer3/local/Vienna results before mandatory peer scoring."""

    completed = 0
    remaining_extra = {
        kind: ensemble_plan.kind(kind).allocated - baseline
        for kind, baseline in (
            ("hairpin", len(oligos)), ("self_dimer", len(oligos)),
            ("hetero_dimer", len(oligos) * (len(oligos) - 1) // 2))
    }

    def record_limit(kind: str, variants: int) -> int:
        # Allocation visits concrete contexts in order, omitting one unknown
        # representative. K+1 records suffice for K extra slots wherever that
        # representative lands. No extras means no retained context records.
        allocated = min(remaining_extra[kind], variants - 1)
        remaining_extra[kind] -= allocated
        return allocated + 1 if allocated else 0

    _raise_if_cancelled(cancel_event)
    _emit_progress(progress_callback, completed, total, "Preparing analysis")
    defer_token = _DEFER_EXTERNAL.set(True)
    try:
        # Tm work has no later engine phase, so it remains a completed
        # progress step immediately. Structure steps complete only after
        # their remaining engine observations are deterministically attached.
        for o in oligos:
            _raise_if_cancelled(cancel_event)
            report.tms.append(_cached_core_result(
                "tm", (o,), cond, lambda o=o: calc_tm(o, cond)))
            completed += 1
            _emit_progress(
                progress_callback, completed, total, f"Tm: {o.name}")

        for oligo_index, o in enumerate(oligos):
            _raise_if_cancelled(cancel_event)
            report.hairpins.append(_cached_core_result(
                "hairpin", (o,), cond,
                lambda o=o, oligo_index=oligo_index: _with_coverage_scope(
                    ("hairpin", oligo_index),
                    lambda: analyze_hairpin(o, cond)),
                coverage_scope=("hairpin", oligo_index),
                primer3_record_limit=record_limit("hairpin", o.n_variants)))
        for oligo_index, o in enumerate(oligos):
            _raise_if_cancelled(cancel_event)
            report.self_dimers.append(_cached_core_result(
                "self-dimer", (o,), cond,
                lambda o=o, oligo_index=oligo_index: _with_coverage_scope(
                    ("self_dimer", oligo_index),
                    lambda: analyze_self_dimer(o, cond)),
                coverage_scope=("self_dimer", oligo_index),
                primer3_record_limit=record_limit("self_dimer", o.n_variants)))
        for left_index, right_index in combinations(range(len(oligos)), 2):
            _raise_if_cancelled(cancel_event)
            a, b = oligos[left_index], oligos[right_index]
            report.hetero_dimers.append(_cached_core_result(
                "hetero-dimer", (a, b), cond,
                lambda a=a, b=b, left_index=left_index,
                right_index=right_index: _with_coverage_scope(
                    ("hetero_dimer", left_index, right_index),
                    lambda: analyze_hetero(a, b, cond)),
                coverage_scope=(
                    "hetero_dimer", left_index, right_index),
                primer3_record_limit=record_limit(
                    "hetero_dimer", a.n_variants * b.n_variants)))
    finally:
        _DEFER_EXTERNAL.reset(defer_token)

    ensemble_plan = _resolve_ensemble_plan_contexts(
        oligos, report, ensemble_plan)
    report.ensemble_plan = ensemble_plan
    report.diagnostics = tuple(diagnostics)
    report.phase = "core_complete"
    if partial_callback is not None:
        partial_callback(_filtered_partial_report(report, cond))
    return ensemble_plan, completed


def _run_mandatory_engine_phase(
    oligos: list[Oligo], cond: ReactionConditions, report: AnalysisReport,
    ensemble_plan: EnsembleWorkPlan,
    progress_callback: Optional[ProgressCallback],
    cancel_event: Optional[CancelEvent],
    max_external_workers: int | None,
    diagnostics: list[sm.ScientificDiagnostic],
    coverage_outcomes: list[dict[str, object]],
    completed: int, total: int,
    primer3_identity: Mapping[str, str],
    vienna_identity: Mapping[str, str],
    vienna_version: str,
) -> None:
    """Attach mandatory peer-engine observations and publish the final report."""

    external_tasks = _build_external_tasks(oligos, report, cond)
    if ensemble_plan.mode == ENSEMBLE_MODE_BUDGETED_COMPLETE:
        external_tasks.extend(_build_additional_external_tasks(
            oligos, report, cond, ensemble_plan, cancel_event))

    workers = _external_worker_count(max_external_workers)
    with ee.RNAstructureSession(cancel_event) as session:
        batches: list[ee.EngineBatch]
        if workers == 1 or len(external_tasks) <= 1:
            batches = [
                _external_task_batch(task, session, cancel_event)
                for task in external_tasks
            ]
        else:
            batches = _bounded_parallel_map(
                list(external_tasks),
                lambda task: _external_task_batch(
                    task, session, cancel_event),
                workers=workers,
                thread_name_prefix="rnastructure",
                cancel_event=cancel_event)

        finalized = _finalize_external_tasks(
            external_tasks, batches, session, cancel_event, workers)

        _publish_finalized_ensemble_tasks(
            report, external_tasks, finalized, cond, ensemble_plan)
        for task, item in zip(external_tasks, finalized):
            diagnostics.extend(item.diagnostics)
            coverage_outcomes.extend(item.coverage_outcomes)
            completed += 1
            _emit_progress(
                progress_callback, completed, total, task.message)

        report.diagnostics = tuple(diagnostics)
        report.ensemble_coverage = build_ensemble_coverage(
            ensemble_plan, coverage_outcomes)
        report.complete = True
        report.phase = "complete"
        report.manifest = create_scientific_manifest(
            oligos, cond, report.diagnostics,
            rnastructure_session=session,
            ensemble_plan=ensemble_plan,
            ensemble_coverage=report.ensemble_coverage,
            primer3_identity=primer3_identity,
            vienna_identity=vienna_identity,
            vienna_version=vienna_version)


def analyze(
    oligos: list[Oligo], cond: ReactionConditions,
    progress_callback: Optional[ProgressCallback] = None,
    cancel_event: Optional[CancelEvent] = None, *,
    partial_callback: Optional[PartialReportCallback] = None,
    max_external_workers: int | None = None,
    ensemble_mode: str = ENSEMBLE_MODE_REPRESENTATIVE,
    ensemble_additional_budget: int = DEFAULT_ENSEMBLE_ADDITIONAL_BUDGET,
) -> AnalysisReport:
    """Run a configured analysis and return its complete, manifested report.

    Keep input oligos and conditions stable until this call returns. Callbacks
    execute synchronously on the calling thread; their exceptions propagate.
    Progress reports preparation, completed Tm work, and deterministically
    published mandatory-engine tasks. The optional partial callback receives
    one detached, non-exportable preview after core calculations and before
    mandatory peer scoring. Mutating that preview cannot change the run.

    Cancellation raises AnalysisCancelled at cooperative work boundaries and
    supported native checkpoints; an in-flight backend call may delay it.
    Failure or cancellation never returns a completed report. Completion means
    all configured tasks finished, while ensemble_coverage separately states
    whether every concrete context was covered successfully. Budgeted runs can
    complete with omitted contexts and must not imply an ensemble-wide worst
    case. Allocated contexts retain their own calculated Primer3 geometry and
    metrics; selecting a representative does not replace those observations.
    """
    ensemble_plan = plan_ensemble_work(
        oligos, mode=ensemble_mode,
        additional_budget=ensemble_additional_budget)
    _raise_if_cancelled(cancel_event)
    primer3_identity = sm.validated_primer3_runtime_identity(primer3)
    ee.require_mandatory_engines()
    vienna_identity = sm.validated_vienna_runtime_identity()
    vb.require_operational(cond)
    vienna_version = vienna_identity["version"]
    diagnostics: list[sm.ScientificDiagnostic] = []
    token = _ACTIVE_DIAGNOSTICS.set(diagnostics)
    coverage_outcomes: list[dict[str, object]] = []
    coverage_token = _ACTIVE_COVERAGE_OUTCOMES.set(coverage_outcomes)
    records_token = _ACTIVE_PRIMER3_RECORDS.set({})
    report = AnalysisReport(
        complete=False, phase="core_running", ensemble_plan=ensemble_plan)
    total = len(oligos) + ensemble_plan.allocated_contexts

    try:
        ensemble_plan, completed = _run_core_analysis_phase(
            oligos, cond, report, ensemble_plan, progress_callback,
            cancel_event, partial_callback, diagnostics, total)
        _run_mandatory_engine_phase(
            oligos, cond, report, ensemble_plan, progress_callback,
            cancel_event, max_external_workers, diagnostics,
            coverage_outcomes, completed, total, primer3_identity,
            vienna_identity, vienna_version)
    finally:
        _ACTIVE_PRIMER3_RECORDS.reset(records_token)
        _ACTIVE_COVERAGE_OUTCOMES.reset(coverage_token)
        _ACTIVE_DIAGNOSTICS.reset(token)
    return report


# --------------------------------------------------------------------------- #
# Plain-text report (for export / CLI)
# --------------------------------------------------------------------------- #
def _severity_flag(severity: str) -> str:
    return {"ok": "  ", "caution": "! ", "problem": "**",
            "unclassified": "? "}[severity]


def _optional_dg_fragment(label: str, value: Optional[float]) -> str:
    if value is None:
        return ""
    return f"  dG({label})={value:6.2f}"


def _secondary_dg_fragments(s) -> str:
    fragments = [_optional_dg_fragment(
        "Vienna", getattr(s, "dg_vienna", None))]
    for engine in ee.ENGINE_ORDER:
        dg, tm = ee.engine_metrics(
            getattr(s, "engine_observations", ()))[engine]
        if dg is not None:
            fragments.append(_optional_dg_fragment(engine, dg))
        if tm is not None:
            tm_label = engine
            if engine == "RNAstructure":
                tm_label += (" fixed CT" if getattr(s, "kind", "") == "Hairpin"
                             else " P3-style dimer")
            fragments.append(f"  Tm({tm_label})={tm:6.2f} C")
    return "".join(fragments)


def _structure_tm_fragment(s) -> str:
    tm_c = getattr(s, "tm_c", None)
    tm_vienna = getattr(s, "tm_vienna_c", None)
    if tm_c is None:
        if tm_vienna is None:
            return "Tm(P3/Vienna)=unavailable"
        return f"Tm Vienna fixed-structure dG=0={tm_vienna:6.2f} C"
    if tm_vienna is not None:
        return (f"Tm Primer3={tm_c:6.2f} C  "
                f"Tm Vienna fixed-structure dG=0={tm_vienna:6.2f} C")
    if "fixed structure" in getattr(s, "thermo_model", "").lower():
        return f"Tm fixed-structure dG=0={tm_c:6.2f} C"
    if getattr(s, "kind", "") == "Hairpin":
        return f"Tm Primer3={tm_c:6.2f} C"
    return f"Tm={tm_c:6.2f} C"


def structure_origin(structure, separator: str = ", ") -> str:
    """Return a truthful public origin label for an exact geometry."""

    discoverers = tuple(getattr(structure, "discovered_by", ()))
    if discoverers:
        return separator.join(discoverers)
    if getattr(structure, "derivation_lineage", ()):
        return "Gap-policy derived"
    return "-"


def _append_peer_structure(lines: list[str], s: PeerStructure) -> None:
    w = lines.append
    flag = _severity_flag(s.severity)
    if not s.found:
        w(f"{flag}{s.label:30s} {s.assessment}")
        return

    p3_dg = "n/a" if s.dg_p3 is None else f"{s.dg_p3:.2f}"
    w(f"{flag}{s.label:30s} dG(P3)={p3_dg} kcal/mol"
      f"{_secondary_dg_fragments(s)}  {_structure_tm_fragment(s)}  "
      f"[{s.severity.upper()}]  origin: {structure_origin(s)}")
    w(f"     {s.assessment}")
    if s.n_combos > 1 and s.representative_variant:
        w(f"     representative variant: {s.representative_variant}")
    if sd.is_drawable(s):
        structure_text = sd.display_ascii(s)
        for line in structure_text.splitlines():
            w(f"       {line}")


def _append_structure_section(lines: list[str], title: str,
                              items: list[InteractionResult]) -> None:
    w = lines.append
    w(title)
    w("-" * 74)
    for interaction in items:
        if not interaction.structures:
            w(f"  {interaction.kind}: {interaction.label}  no structure found")
        for peer in interaction.structures:
            _append_peer_structure(lines, peer)
    w("")


def _append_summary_findings(lines: list[str], marker: str,
                             findings: list) -> None:
    w = lines.append
    for s in findings:
        drivers = ", ".join(s.severity_drivers) or "structure policy"
        w(f"    {marker} {s.label} "
          f"(severity drivers: {drivers})")


def _append_summary(lines: list[str], report: AnalysisReport) -> None:
    w = lines.append
    w("Summary")
    w("-" * 74)
    w(f"  Problems: {len(report.problems)}    Cautions: {len(report.cautions)}")
    if report.problems:
        _append_summary_findings(lines, "**", report.problems)
    if report.cautions:
        _append_summary_findings(lines, "! ", report.cautions)
    if not report.problems and not report.cautions:
        w("    No significant secondary-structure liabilities detected.")
    w("=" * 74)


_DELIMITED_COLUMNS = [
    "record_type",
    "group",
    "parameter",
    "value",
    "unit",
    "rank",
    "discovered_by",
    "structure_origin",
    "kind",
    "label",
    "oligo_name",
    "role",
    "sequence",
    "length",
    "variant_count",
    "gc_percent",
    "concentration_nm",
    "tm_mean_c",
    "tm_min_c",
    "tm_max_c",
    "tm_owczarzy_mean_c",
    "tm_owczarzy_min_c",
    "tm_owczarzy_max_c",
    "structure_found",
    "dg_p3_kcal_mol",
    "dg_vienna_kcal_mol",
    "dg_rnastructure_kcal_mol",
    "tm_rnastructure_c",
    "dg_seqfold_kcal_mol",
    "tm_seqfold_c",
    "dg_rnastructure_state",
    "tm_rnastructure_state",
    "dg_seqfold_state",
    "tm_seqfold_state",
    "structure_tm_c",
    "structure_tm_vienna_c",
    "severity",
    "site",
    "assessment",
    "n_combos",
    "representative_variant",
    "structure",
    "thermo_model",
    "engine_observations_json",
    "pair_classes_json",
    "geometry_diagnostics_json",
    "derivation_lineage_json",
    "ensemble_complete",
    "ensemble_coverage_json",
    "manifest_json",
]


def _blank_delimited_row(record_type: str, group: str) -> dict[str, str]:
    row = {col: "" for col in _DELIMITED_COLUMNS}
    row["record_type"] = record_type
    row["group"] = group
    return row


def _fmt_number(value: Optional[float], digits: int = 2) -> str:
    if value is None:
        return ""
    return f"{value:.{digits}f}"


def _append_condition_rows(rows: list[dict[str, str]],
                           cond: ReactionConditions) -> None:
    salt = nn.derive_salt(cond)
    for parameter, value, unit in [
        ("mv_conc", cond.mv_conc, "mM"),
        ("dv_conc", cond.dv_conc, "mM"),
        ("dntp_conc", cond.dntp_conc, "mM"),
        ("primer_conc", cond.primer_conc, "nM"),
        ("probe_conc", cond.probe_conc, "nM"),
        ("dg_temp_c", cond.dg_temp_c, "C"),
        ("dg_caution", cond.dg_caution, "kcal/mol"),
        ("dg_problem", cond.dg_problem, "kcal/mol"),
        ("near_duplicate_bond_difference",
         cond.near_duplicate_bond_difference, "bonds"),
        ("dimer_max_consecutive_gaps",
         cond.dimer_max_consecutive_gaps, "bases"),
        ("dimer_max_total_gaps", cond.dimer_max_total_gaps, "bases"),
        ("assumed_free_mg", salt.assumed_free_mg_mM, "mM"),
        ("effective_na_equivalent", salt.effective_na_equivalent_M, "M"),
        ("salt_formula_id", salt.formula_id, "policy"),
        ("salt_clamping_applied", str(salt.clamping_applied).lower(), "boolean"),
    ]:
        row = _blank_delimited_row("condition", "reaction_conditions")
        row["parameter"] = parameter
        row["value"] = f"{value:g}" if isinstance(value, (int, float)) else value
        row["unit"] = unit
        rows.append(row)


def _append_tm_rows(rows: list[dict[str, str]],
                    report: AnalysisReport) -> None:
    for tm in report.tms:
        o = tm.oligo
        row = _blank_delimited_row("tm", "melting_temperatures")
        row.update({
            "label": o.name,
            "oligo_name": o.name,
            "role": o.role,
            "sequence": o.seq,
            "length": str(o.length),
            "variant_count": str(o.n_variants),
            "gc_percent": o.gc_display(),
            "concentration_nm": f"{o.conc_nM:g}",
            "tm_mean_c": _fmt_number(tm.tm_mean),
            "tm_min_c": _fmt_number(tm.tm_min),
            "tm_max_c": _fmt_number(tm.tm_max),
            "tm_owczarzy_mean_c": _fmt_number(tm.tm_owczarzy_mean),
            "tm_owczarzy_min_c": _fmt_number(tm.tm_owczarzy_min),
            "tm_owczarzy_max_c": _fmt_number(tm.tm_owczarzy_max),
        })
        rows.append(row)


def _structure_sequence(s) -> str:
    representative = getattr(s, "representative_variant", "")
    if representative:
        return representative
    if getattr(s, "worst_variant", ""):  # legacy pre-schema-3 objects
        return s.worst_variant
    if getattr(s, "hp_seq", ""):
        return s.hp_seq
    if getattr(s, "du_a", "") or getattr(s, "du_b", ""):
        return f"{s.du_a} / {s.du_b}"
    return ""


def _structure_text(s) -> str:
    if sd.is_drawable(s):
        return sd.display_ascii(s)
    return getattr(s, "structure", "") or getattr(s, "ascii_text", "")


def _delimited_structure_row(record_type: str, group: str, rank: str,
                             s) -> dict[str, str]:
    found = getattr(s, "found", True)
    row = _blank_delimited_row(record_type, group)
    row.update({
        "rank": rank,
        "discovered_by": ";".join(getattr(s, "discovered_by", ())),
        "structure_origin": structure_origin(s, ";"),
        "kind": s.kind,
        "label": s.label,
        "sequence": _structure_sequence(s),
        "structure_found": "yes" if found else "no",
        "severity": s.severity,
        "assessment": s.assessment,
        "n_combos": str(getattr(s, "n_combos", "")),
        "representative_variant": getattr(
            s, "representative_variant", ""),
        "structure": _structure_text(s),
        "thermo_model": getattr(s, "thermo_model", ""),
    })
    if found:
        row.update({
            "dg_p3_kcal_mol": _fmt_number(s.dg_kcal),
            "dg_vienna_kcal_mol": _fmt_number(s.dg_vienna),
            "structure_tm_c": _fmt_number(getattr(s, "tm_c", None)),
            "structure_tm_vienna_c": _fmt_number(
                getattr(s, "tm_vienna_c", None)),
            "site": "3' end" if s.involves_3prime else "internal",
        })
    metrics = ee.engine_metrics(getattr(s, "engine_observations", ()))
    column_stems = {
        "RNAstructure": "rnastructure",
        "seqfold": "seqfold",
    }
    for engine, stem in column_stems.items():
        dg_value, tm_value = metrics[engine]
        row[f"dg_{stem}_kcal_mol"] = _fmt_number(dg_value)
        row[f"tm_{stem}_c"] = _fmt_number(tm_value)
        states = getattr(s, "external_metric_states", {}).get(engine, {})
        row[f"dg_{stem}_state"] = states.get("dg", "")
        row[f"tm_{stem}_state"] = states.get("tm", "")
    row["engine_observations_json"] = ee.observations_json(
        getattr(s, "engine_observations", ()))
    row["pair_classes_json"] = sm.canonical_json(
        list(getattr(s, "pair_classes", ())))
    row["geometry_diagnostics_json"] = sm.canonical_json(
        list(getattr(s, "geometry_diagnostics", ())))
    row["derivation_lineage_json"] = sm.canonical_json(
        list(getattr(s, "derivation_lineage", ())))
    return row


def _append_structure_rows(rows: list[dict[str, str]], group: str,
                           items: list[InteractionResult]) -> None:
    for interaction in items:
        for peer in interaction.structures:
            rows.append(_delimited_structure_row(
                "structure", group, str(peer.rank), peer))


def _delimited_report_rows(oligos: list[Oligo],
                           report: AnalysisReport,
                           cond: ReactionConditions) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    manifest = ensure_report_manifest(oligos, report, cond)
    manifest_row = _blank_delimited_row(
        "analysis_manifest", "scientific_provenance")
    manifest_row["manifest_json"] = manifest.canonical_json()
    if report.ensemble_coverage is not None:
        coverage_payload = report.ensemble_coverage.to_dict()
        manifest_row["ensemble_complete"] = str(
            coverage_payload["ensemble_complete"]).lower()
        manifest_row["ensemble_coverage_json"] = sm.canonical_json(
            coverage_payload)
    rows.append(manifest_row)
    _append_condition_rows(rows, cond)
    _append_tm_rows(rows, report)
    _append_structure_rows(rows, "hairpins", report.hairpins)
    _append_structure_rows(rows, "self_dimers", report.self_dimers)
    _append_structure_rows(rows, "hetero_dimers", report.hetero_dimers)
    return rows


def format_delimited_report(oligos: list[Oligo],
                            report: AnalysisReport,
                            cond: ReactionConditions,
                            delimiter: str = ",") -> str:
    """Render the report as a spreadsheet-friendly CSV/TSV table."""
    if delimiter not in {",", "\t"}:
        raise SequenceError("delimiter must be ',' for CSV or '\\t' for TSV")

    out = io.StringIO(newline="")
    writer = csv.DictWriter(
        out,
        fieldnames=_DELIMITED_COLUMNS,
        delimiter=delimiter,
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(_delimited_report_rows(oligos, report, cond))
    return out.getvalue()


def format_csv_report(oligos: list[Oligo],
                      report: AnalysisReport,
                      cond: ReactionConditions) -> str:
    return format_delimited_report(oligos, report, cond, ",")


def format_tsv_report(oligos: list[Oligo],
                      report: AnalysisReport,
                      cond: ReactionConditions) -> str:
    return format_delimited_report(oligos, report, cond, "\t")


def format_text_report(oligos: list[Oligo],
                       report: AnalysisReport,
                       cond: ReactionConditions) -> str:
    """Render plain text under ensure_report_manifest's export contract."""
    L: list[str] = []
    w = L.append
    manifest = ensure_report_manifest(oligos, report, cond)
    w("=" * 74)
    w("  PCR PRIMER / PROBE Tm & SECONDARY-STRUCTURE REPORT")
    w("=" * 74)
    w("")
    if report.ensemble_coverage is not None:
        coverage = report.ensemble_coverage.to_dict()
        w("Degenerate ensemble coverage")
        w("-" * 74)
        w("  ensemble_complete=" + str(
            coverage["ensemble_complete"]).lower())
        w(f"  concrete contexts: {coverage['evaluated_contexts']}/"
          f"{coverage['total_contexts']}")
        if not coverage["ensemble_complete"]:
            w("  PARTIAL: omitted variants may contain stronger structures;")
            w("           this report is not a worst-case statement for the full ensemble.")
        for engine, kinds in coverage["engines"].items():
            values = [
                f"{kind}={entry['evaluated']}/{entry['total']}"
                + (f" failed={entry['failed']}" if entry["failed"] else "")
                for kind, entry in kinds.items() if entry["supported"]
            ]
            w(f"  {engine}: " + "; ".join(values))
        w("")
    w("Reaction conditions")
    w("-" * 74)
    w(f"  Monovalent cations (Na+/K+) : {cond.mv_conc:g} mM")
    w(f"  Divalent cations   (Mg2+)   : {cond.dv_conc:g} mM")
    w(f"  dNTPs                       : {cond.dntp_conc:g} mM")
    salt = nn.derive_salt(cond)
    w(f"  Assumed free Mg2+           : {salt.assumed_free_mg_mM:g} mM")
    w(f"  Effective Na-equivalent     : {salt.effective_na_equivalent_M:g} M")
    w(f"  Salt formula                : {salt.formula_id}")
    w(f"  Salt clamping applied       : {'yes' if salt.clamping_applied else 'no'}")
    w(f"  Primer concentration        : {cond.primer_conc:g} nM")
    w(f"  Probe concentration         : {cond.probe_conc:g} nM")
    w(f"  dG simulation temperature   : {cond.dg_temp_c:g} C")
    w(f"  Thresholds (kcal/mol, all structure types): "
      f"caution<={cond.dg_caution:g}, problem<={cond.dg_problem:g}")
    w(f"  Near-duplicate bond difference : <{cond.near_duplicate_bond_difference}")
    w(f"  Dimer internal gap limits      : run<={cond.dimer_max_consecutive_gaps}, "
      f"total<={cond.dimer_max_total_gaps}")
    if report.has_degenerate:
        w("  NOTE: degenerate oligos expanded to all A/C/G/T variants;")
        if report.ensemble_complete:
            w("        Tm shown as range; peer engines covered the complete ensemble.")
        else:
            w("        Tm shown as range; peer-engine structure coverage is partial.")
    if any(s.dg_vienna is not None for s in report.all_findings):
        w("  dG(P3) = Primer3 two-state thal; dG(Vienna) = ViennaRNA loop model;")
        w("           every hairpin metric belongs to its displayed full-length geometry;")
        w("           their Tm is the 0-100 C dG=0 crossing, not Primer3 thal Tm.")
        w("           Dimer side opinions are binding free energies where supported.")
    w("")
    w("Machine-readable analysis manifest")
    w("ANALYSIS_MANIFEST_JSON_BEGIN")
    w(manifest.canonical_json())
    w("ANALYSIS_MANIFEST_JSON_END")
    w("")
    w("Melting temperatures")
    w("-" * 74)
    w(f"  {'Oligo':16s} {'Len':>3s} {'Var':>4s} {'GC%':>10s} "
      f"{'Conc(nM)':>9s} {'Tm SL':>13s} {'Tm Owcz':>13s}   Sequence")
    for t in report.tms:
        o = t.oligo
        w(f"  {o.name:16s} {o.length:3d} {o.n_variants:4d} {o.gc_display():>10s} "
          f"{o.conc_nM:9g} {t.tm_display():>13s} "
          f"{t.tm_owczarzy_display():>13s}   {o.seq}")
    w("")

    _append_structure_section(L, "Hairpins", report.hairpins)
    _append_structure_section(L, "Self-dimers (homodimers)", report.self_dimers)
    if report.hetero_dimers:
        _append_structure_section(L, "Hetero-dimers (cross-dimers)",
                                  report.hetero_dimers)

    _append_summary(L, report)
    return "\n".join(L)
