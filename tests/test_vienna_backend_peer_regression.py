from unittest.mock import patch

import thermo_engine as te
import vienna_backend as vb


def test_hairpin_dg_returns_the_single_strand_mfe():
    diagnostics = []
    with patch.object(vb, "_fold_mfe_cached", return_value=-2.75) as fold:
        value = vb.hairpin_dg("GGGAAACCC", te.ReactionConditions(), diagnostics)

    assert value == -2.75
    assert fold.call_count == 1
    assert diagnostics == []


def test_dimer_dg_returns_binding_energy_not_raw_cofold_energy():
    diagnostics = []
    with patch.object(vb, "_dimer_mfe_cached", return_value=-8.0), \
         patch.object(vb, "_fold_mfe_cached", side_effect=(-1.5, -2.0)):
        value = vb.dimer_dg(
            "AAAAA", "TTTTT", te.ReactionConditions(), diagnostics)

    assert value == -4.5
    assert diagnostics == []


def _fixed_hairpin_tm(energy_at_temperature):
    with patch.object(vb, "_OK", True), \
         patch.object(
             vb, "_hairpin_structure_energy_cached",
             side_effect=lambda _seq, _structure, temperature, _salt:
             energy_at_temperature(temperature),
         ):
        return vb.hairpin_structure_tm(
            "GGGAAACCC", "///---\\\\\\", te.ReactionConditions())


def test_hairpin_tm_accepts_one_exact_zero_on_a_monotonic_curve():
    result = _fixed_hairpin_tm(lambda temperature: temperature - 50.0)

    assert result.status == "crossing_found"
    assert result.value == 50.0
    assert result.bracket == (50.0, 50.0)


def test_hairpin_tm_rejects_exact_zero_on_a_nonmonotonic_curve():
    def energy(temperature):
        if temperature == 50.0:
            return 0.0
        if temperature == 55.0:
            return -2.0
        return temperature - 50.0

    result = _fixed_hairpin_tm(energy)

    assert result.status == "nonmonotonic_or_multiple_crossings"
    assert result.value is None
    assert result.bracket is None


def test_hairpin_tm_rejects_multiple_exact_zero_samples():
    def energy(temperature):
        if temperature in (45.0, 55.0):
            return 0.0
        return temperature - 50.0

    result = _fixed_hairpin_tm(energy)

    assert result.status == "nonmonotonic_or_multiple_crossings"
    assert result.value is None
    assert result.bracket is None


def test_hairpin_tm_rejects_an_all_zero_plateau():
    result = _fixed_hairpin_tm(lambda _temperature: 0.0)

    assert result.status == "nonmonotonic_or_multiple_crossings"
    assert result.value is None
    assert result.bracket is None
