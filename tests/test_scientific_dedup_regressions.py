"""Behavioral regressions for cross-context scientific deduplication."""

from __future__ import annotations

import external_engines as ee
import thermo_engine as te


def _dimer_peer(
    geometry: ee.CanonicalGeometry, dg: float, representative: str,
) -> te.PeerStructure:
    return te.PeerStructure(
        kind=("Self-dimer" if geometry.kind == "self-dimer"
              else "Hetero-dimer"),
        label=representative,
        found=True,
        geometry=geometry,
        structure="fixture",
        discovered_by=("RNAstructure",),
        dg_p3=dg,
        representative_variant=representative,
    )


def _logical_survivors(*peers: te.PeerStructure) -> list[te.PeerStructure]:
    return te._suppress_near_duplicate_peers(
        list(peers), 4, comparison_scope="logical_interaction")


def test_logical_comparison_collapses_same_participant_bonds_after_lexical_flip():
    pairs = ((0, 5), (1, 4), (2, 3))
    direct_canonical = ee.CanonicalGeometry(
        "hetero-dimer", "AAAAAA", "CCCCCC", pairs)
    swapped_canonical = ee.CanonicalGeometry(
        "hetero-dimer", "GGGGGG", "CCCCCC", pairs)

    less_adverse = _dimer_peer(direct_canonical, -7.0, "A1 / B1")
    more_adverse = _dimer_peer(swapped_canonical, -8.0, "A2 / B1")

    assert _logical_survivors(less_adverse, more_adverse) == [more_adverse]


def test_logical_comparison_keeps_distinct_participant_bonds_after_lexical_flip():
    pairs = ((0, 5), (1, 4), (2, 3))
    direct_canonical = ee.CanonicalGeometry(
        "hetero-dimer", "AAAAAA", "CCCCCC", pairs)
    swapped_pairs = tuple((right, left) for left, right in pairs)
    swapped_canonical = ee.CanonicalGeometry(
        "hetero-dimer", "GGGGGG", "CCCCCC", swapped_pairs)

    first = _dimer_peer(direct_canonical, -7.0, "A1 / B1")
    second = _dimer_peer(swapped_canonical, -8.0, "A2 / B1")

    assert _logical_survivors(first, second) == [first, second]


def test_logical_self_dimer_comparison_preserves_strand_swap_symmetry():
    pairs = ((0, 5), (1, 4), (2, 3))
    direct = ee.CanonicalGeometry("self-dimer", "ACGTAC", "ACGTAC", pairs)
    swapped = ee.CanonicalGeometry(
        "self-dimer", "ACGTAC", "ACGTAC",
        tuple((right, left) for left, right in pairs))

    less_adverse = _dimer_peer(direct, -7.0, "variant-direct")
    more_adverse = _dimer_peer(swapped, -8.0, "variant-swapped")

    assert _logical_survivors(less_adverse, more_adverse) == [more_adverse]
