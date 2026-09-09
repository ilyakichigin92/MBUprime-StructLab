"""Deterministic contiguous candidate generation for LocalEnumerator.

This module enumerates gapless Watson-Crick hairpin stems and duplex registers
that backend-selected folds may omit. SantaLucia-style local estimates order
candidate generation only: LocalEnumerator is not a thermodynamic engine and
its scores are never reported as exact dG or Tm. Retained geometries are
rescored by the applicable mandatory engines in ``thermo_engine``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# SantaLucia 1998 unified NN parameters, keyed by the 5'->3' dinucleotide of one
# strand (its Watson-Crick complement is implied). (dH kcal/mol, dS cal/mol/K).
NN = {
    "AA": (-7.6, -21.3), "TT": (-7.6, -21.3),
    "AT": (-7.2, -20.4), "TA": (-7.2, -21.3),
    "CA": (-8.5, -22.7), "TG": (-8.5, -22.7),
    "GT": (-8.4, -22.4), "AC": (-8.4, -22.4),
    "CT": (-7.8, -21.0), "AG": (-7.8, -21.0),
    "GA": (-8.2, -22.2), "TC": (-8.2, -22.2),
    "CG": (-10.6, -27.2), "GC": (-9.8, -24.4),
    "GG": (-8.0, -19.9), "CC": (-8.0, -19.9),
}
INIT_GC = (0.1, -2.8)      # initiation per terminal G.C pair
INIT_AT = (2.3, 4.1)       # initiation per terminal A.T pair

# Approximate hairpin-loop initiation dG (kcal/mol, 37C) by loop size.
_HAIRPIN_LOOP = {3: 5.4, 4: 5.6, 5: 5.7, 6: 5.4, 7: 6.0, 8: 5.5, 9: 6.4}
_R = 1.9872e-3             # gas constant kcal/mol/K

_COMP = {"A": "T", "T": "A", "G": "C", "C": "G"}


def _wc(a: str, b: str) -> bool:
    return _COMP.get(a) == b


@dataclass(frozen=True)
class SaltDerivation:
    """Explicit chemistry derived for the NN and Vienna model paths."""

    assumed_free_mg_mM: float
    effective_na_equivalent_M: float
    formula_id: str = "na_equiv_owczarzy_120_sqrt_free_mg_v1"
    clamping_applied: bool = False


def derive_salt(cond) -> SaltDerivation:
    """Derive monovalent-equivalent salt without hidden floors or clamps."""

    mv = float(cond.mv_conc)
    free_dv = float(cond.dv_conc) - float(cond.dntp_conc)
    if mv <= 0.0:
        raise ValueError("mv_conc must be > 0 for salt correction")
    if free_dv <= 0.0:
        raise ValueError("free Mg2+ (dv_conc - dntp_conc) must be > 0")
    na_mM = mv + 120.0 * math.sqrt(free_dv)
    return SaltDerivation(
        assumed_free_mg_mM=free_dv,
        effective_na_equivalent_M=na_mM / 1000.0,
    )


def na_equiv_M(cond) -> float:
    """Monovalent-equivalent [Na+] in mol/L from reaction conditions."""

    return derive_salt(cond).effective_na_equivalent_M


def _loop_dg(n: int, temp_k: float) -> float:
    if n in _HAIRPIN_LOOP:
        return _HAIRPIN_LOOP[n]
    if n < 3:
        return _HAIRPIN_LOOP[3]
    return _HAIRPIN_LOOP[9] + 1.75 * _R * temp_k * math.log(n / 9.0)


def dg_duplex(top: str, cond, temp_c: float) -> float:
    """Return a local ranking estimate in kcal/mol at temp_c (Celsius).

    top is one 5'->3' strand of a contiguous Watson-Crick helix (length >= 2).
    This candidate-ranking estimate is not an engine-owned report metric.
    """
    dH = dS = 0.0
    for i in range(len(top) - 1):
        h, s = NN[top[i:i + 2]]
        dH += h
        dS += s
    for term in (top[0], top[-1]):
        h, s = INIT_AT if term in "AT" else INIT_GC
        dH += h
        dS += s
    dS += 0.368 * (len(top) - 1) * math.log(na_equiv_M(cond))
    temp_k = temp_c + 273.15
    return dH - temp_k * dS / 1000.0


def dg_hairpin(arm5: str, loop_len: int, cond, temp_c: float) -> float:
    """Return a local hairpin-ranking estimate in kcal/mol at Celsius temp_c.

    Combine 5' arm stacking and a loop penalty for candidate selection; exact
    engine scoring supplies report metrics after geometry finalization.
    """
    dH = dS = 0.0
    for i in range(len(arm5) - 1):
        h, s = NN[arm5[i:i + 2]]
        dH += h
        dS += s
    for term in (arm5[0], arm5[-1]):
        h, s = INIT_AT if term in "AT" else INIT_GC
        dH += h
        dS += s
    dS += 0.368 * (len(arm5) - 1) * math.log(na_equiv_M(cond))
    temp_k = temp_c + 273.15
    return (dH - temp_k * dS / 1000.0) + _loop_dg(loop_len, temp_k)


# --------------------------------------------------------------------------- #
# Enumerated-structure container
# --------------------------------------------------------------------------- #
@dataclass
class SubStructure:
    kind: str                 # "Hairpin" | "Self-dimer" | "Hetero-dimer"
    label: str
    # Provisional local ranking estimate in kcal/mol, not a Primer3 result.
    # Do not copy it into reported exact-geometry engine metrics.
    dg_kcal: float | None
    involves_3prime: bool
    mode: str                 # "hairpin" | "duplex"
    ascii_text: str
    tm_c: float | None = None # model-specific structure Tm, if available
    dg_vienna: float | None = None   # second-opinion dG (filled by the engine)
    thermo_model: str = ""
    severity: str = "ok"
    assessment: str = ""
    # hairpin rendering data
    hp_seq: str = ""
    hp_pattern: str = ""
    # duplex rendering data
    du_a: str = ""
    du_b: str = ""
    du_offset: int = 0
    du_pairs: tuple = field(default_factory=tuple)   # ((a_idx, b_idx), ...)
    # Geometry-owned thermodynamic-engine observations are merged by the peer
    # finalizer;
    # eligibility for severity is defined there by engine, quantity, and status.
    engine_observations: tuple = field(default_factory=tuple)
    external_diagnostics: tuple[str, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------- #
# Hairpin enumeration
# --------------------------------------------------------------------------- #
def enumerate_hairpins(seq: str, cond, top_n: int,
                       min_stem: int = 3, min_loop: int = 3) -> list[SubStructure]:
    temp_c = cond.dg_temp_c
    L = len(seq)
    found: dict[tuple, SubStructure] = {}
    for i in range(L):
        for j in range(i + 2 * min_stem + min_loop - 1, L):
            if not _wc(seq[i], seq[j]):
                continue
            # require (i, j) to be the OUTERMOST pair (avoid sub-stems)
            if i - 1 >= 0 and j + 1 < L and _wc(seq[i - 1], seq[j + 1]):
                continue
            k = 0
            while (i + k < j - k and _wc(seq[i + k], seq[j - k])
                   and (j - k) - (i + k) - 1 >= min_loop):
                k += 1
            stem_len = k
            if stem_len < min_stem:
                continue
            loop_len = j - i - 2 * stem_len + 1
            arm5 = seq[i:i + stem_len]
            dg = dg_hairpin(arm5, loop_len, cond, temp_c)
            pattern = list("-" * L)
            for t in range(stem_len):
                pattern[i + t] = "/"
                pattern[j - t] = "\\"
            pattern = "".join(pattern)
            three = (j == L - 1)          # 3' terminus is in the stem
            key = (i, j, stem_len)
            found[key] = SubStructure(
                kind="Hairpin", label="", dg_kcal=dg, involves_3prime=three,
                mode="hairpin", ascii_text=f"{pattern}\n{seq}",
                hp_seq=seq, hp_pattern=pattern)
    subs = sorted(found.values(), key=lambda s: s.dg_kcal)
    return subs[:top_n]


# --------------------------------------------------------------------------- #
# Duplex (dimer) enumeration
# --------------------------------------------------------------------------- #
def _duplex_ascii(A: str, B: str, offset: int, a_pos: list[int]) -> str:
    """Two-row antiparallel alignment (B shown 3'->5', i.e. reversed) with a
    '|' rung line for the paired columns."""
    Br = B[::-1]
    la, lb = len(A), len(Br)
    shift = max(0, -offset)
    top = [" "] * (shift + la)
    for i, ch in enumerate(A):
        top[shift + i] = ch
    width = max(shift + la, offset + shift + lb)
    top += [" "] * (width - len(top))
    bot = [" "] * width
    for k, ch in enumerate(Br):
        bot[offset + shift + k] = ch
    mid = [" "] * width
    for i in a_pos:
        mid[shift + i] = "|"
    top_s = "5'-" + "".join(top) + "-3'"
    mid_s = "   " + "".join(mid)
    bot_s = "3'-" + "".join(bot) + "-5'"
    return f"{top_s}\n{mid_s}\n{bot_s}"


def enumerate_duplex(A: str, B: str, cond, top_n: int,
                     self_dimer: bool = False, min_len: int = 3) -> list[SubStructure]:
    temp_c = cond.dg_temp_c
    Br = B[::-1]
    la, lb = len(A), len(Br)
    cands: dict[tuple, SubStructure] = {}
    kind = "Self-dimer" if self_dimer else "Hetero-dimer"

    def flush(run):
        if len(run) < min_len:
            return
        a0, a1 = run[0], run[-1]
        top = A[a0:a1 + 1]
        dg = dg_duplex(top, cond, temp_c)
        b_pos = [lb - 1 - (i - d) for i in run]          # original B indices
        pairs = tuple(zip(run, b_pos))
        a_risk = {idx for idx in (la - 1, la - 2) if idx >= 0}
        b_risk = {idx for idx in (lb - 1, lb - 2) if idx >= 0}
        three = bool(a_risk.intersection(run) or b_risk.intersection(b_pos))
        if self_dimer:
            key = tuple(sorted(tuple(sorted(p)) for p in pairs))
        else:
            key = pairs
        if key in cands and cands[key].dg_kcal <= dg:
            return
        cands[key] = SubStructure(
            kind=kind, label="", dg_kcal=dg, involves_3prime=three,
            mode="duplex", ascii_text=_duplex_ascii(A, B, d, run),
            du_a=A, du_b=B, du_offset=d, du_pairs=pairs)

    for d in range(-(lb - 1), la):
        i0, i1 = max(0, d), min(la, lb + d)
        run: list[int] = []
        for i in range(i0, i1):
            if _wc(A[i], Br[i - d]):
                run.append(i)
            else:
                flush(run)
                run = []
        flush(run)

    subs = sorted(cands.values(), key=lambda s: s.dg_kcal)
    return subs[:top_n]
