"""Native boundary checks with synthetic motifs and no external fixture files."""

import threading

import pytest

import external_engines as ee
import rnastructure_native


def test_fixed_scoring_preserves_batch_order_and_unpaired_zero():
    backend = rnastructure_native.create_backend()
    paired = ee.CanonicalGeometry(
        "hairpin", "GCGAAACGC", None, ((0, 8), (1, 7), (2, 6)))
    unpaired = ee.CanonicalGeometry("hairpin", "GCGAAACGC", None, ())
    scores = backend.score_fixed_geometries([paired, unpaired, paired], 25.0)
    assert len(scores) == 3
    assert scores[0] == scores[2]
    assert scores[1] == {
        "dg_37_kcal_mol": 0.0, "dh_kcal_mol": 0.0,
        "dg_requested_kcal_mol": 0.0,
    }
    assert scores[0] != scores[1]


def test_live_hairpin_honors_already_requested_cancellation():
    backend = rnastructure_native.create_backend()
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(ee.ExternalEngineCancelled):
        backend.fold_hairpin("GCGAAACGC", 25.0, 10, cancel_event=cancelled)


@pytest.mark.parametrize("pairs", [((0, 99),), ((0, 0),), ((0, 5), (1, 5))])
def test_native_memory_batch_rejects_unsafe_pair_indices(pairs):
    backend = rnastructure_native.create_backend()
    with pytest.raises(RuntimeError, match="invalid fixed-geometry pair"):
        backend._native.score_ct(
            "GCGCGC", str(backend._scaled), str(backend._enthalpy), 310.15,
            pair_batches=(pairs,))
