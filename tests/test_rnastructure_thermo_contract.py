"""Fail-first contract for RNAstructure fixed-geometry thermodynamics.

RNAstructure thermodynamic totals must remain owned by the exact CT geometry
that produced them.  The requested report dG is reconstructed from the
backend's inner dH/dS totals. Hairpin Tm is the intrinsic dG=0 crossing;
dimer Tm applies the declared Primer3-style salt/concentration correction.
Exact-geometry RNAstructure values participate in peer severity and ranking.
"""

from __future__ import annotations

import csv
import io
import json
import math
import pathlib
import types
import unittest
from unittest.mock import patch

import external_engines as ee
import primer_tool_gui as gui
import thermo_engine as te


HAIRPIN_SEQ = "GCGAAACGC"
DIMER_A = "GCG"
DIMER_B = "CGC"
DH = -50.0  # kcal/mol
DS = -150.0  # cal/(mol K)
EXPECTED_TM_C = (1000.0 * DH / DS) - 273.15


def _dg_at(temperature_c: float, dh: float = DH, ds: float = DS) -> float:
    return dh - (temperature_c + 273.15) * ds / 1000.0


def _hairpin_geometry() -> ee.CanonicalGeometry:
    return ee.hairpin_geometry_from_dot_bracket(
        HAIRPIN_SEQ, "(((...)))")


def _rna_observation(geometry: ee.CanonicalGeometry,
                     *, dg: float = -5.2775,
                     tm: float = EXPECTED_TM_C) -> ee.EngineObservation:
    return ee.EngineObservation(
        engine="RNAstructure", engine_version="fixture-6.6", status="ok",
        interaction_type=geometry.kind, search_mode="mfe",
        geometry=geometry, dg_kcal_mol=dg, tm_c=tm,
        energy_definition=("RNAstructure fixed-geometry DNA dG reconstructed "
                           "at requested temperature"),
        tm_definition="intrinsic fixed-geometry dG=0 crossing",
        structure_scope="exact canonical CT geometry",
        structure_text="fixture",
        warning="exact-geometry severity metric",
        provenance="controlled RNAstructure inner dH/dS fixture",
        dh_kcal_mol=DH, ds_cal_mol_k=DS,
        tm_status="crossing_found", tm_bracket_c=(60.18, 60.19),
        tm_search_range_c=(0.0, 100.0),
        tm_numerical_tolerance_c=0.01,
        thermo_path_identity=repr(geometry.identity),
        tm_method="rnastructure_fixed_geometry_inner_dh_ds",
    )


class _NativeFixtureBackend:
    """Deterministic in-process backend seam for adapter thermodynamics."""

    def __init__(self, hairpin_pairs=None, duplex_pairs=None, scores=None,
                 score_error=None):
        self.hairpin_pairs = hairpin_pairs or (_hairpin_geometry().pairs,)
        self.duplex_pairs = duplex_pairs or (((0, 2), (1, 1), (2, 0)),)
        dg37 = _dg_at(37.0)
        self.scores = scores or tuple({
            "dg_37_kcal_mol": dg37,
            "dh_kcal_mol": DH,
            "dg_requested_kcal_mol": _dg_at(25.0),
        } for _item in self.hairpin_pairs)
        self.score_error = score_error
        self.calls = []

    def runtime_identity(self):
        return {"integration": "in_process_native", "engine_version": "6.6",
                "native_module_sha256": "a" * 64,
                "dna_table_manifest_sha256": "b" * 64}

    def fold_hairpin(self, sequence, temperature_c, maximum_structures,
                     cancel_event=None):
        self.calls.append(("fold_hairpin", temperature_c))
        return tuple({"pairs": pairs, "search_dg_kcal_mol": -99.0}
                     for pairs in self.hairpin_pairs)

    def fold_duplex(self, sequence_a, sequence_b, temperature_c,
                    maximum_structures, cancel_event=None):
        self.calls.append(("fold_duplex", temperature_c))
        return tuple({"pairs": pairs, "search_dg_kcal_mol": -99.0}
                     for pairs in self.duplex_pairs)

    def score_fixed_geometries(self, geometries, temperature_c,
                               cancel_event=None):
        self.calls.append(("score_fixed_geometries", temperature_c,
                           tuple(geometries)))
        if self.score_error is not None:
            raise self.score_error
        if len(self.scores) == len(geometries):
            return self.scores
        return tuple(self.scores[0] for _geometry in geometries)


def _native_session(backend):
    return types.SimpleNamespace(
        backend=backend, runtime_status="available", runtime_diagnostic="",
        native_identity=backend.runtime_identity(), cancel_event=None)


class RNAstructureThermodynamicEquationTests(unittest.TestCase):
    def _calculate(self, *, temperature_c=25.0, dh=DH, ds=DS,
                   path_identity=None):
        geometry = _hairpin_geometry()
        return ee._rnastructure_thermo_from_dh_ds(
            geometry=geometry,
            temperature_c=temperature_c,
            dh_kcal_mol=dh,
            ds_cal_mol_k=ds,
            path_identity=(repr(geometry.identity) if path_identity is None
                           else path_identity),
        )

    def test_requested_temperature_uses_kelvin_dg_relation(self):
        at_25 = self._calculate(temperature_c=25.0)
        at_37 = self._calculate(temperature_c=37.0)

        self.assertEqual(at_25.status, "crossing_found")
        self.assertAlmostEqual(at_25.dg_kcal_mol, _dg_at(25.0), places=10)
        self.assertAlmostEqual(at_37.dg_kcal_mol, _dg_at(37.0), places=10)
        self.assertAlmostEqual(
            at_37.dg_kcal_mol - at_25.dg_kcal_mol,
            -(37.0 - 25.0) * DS / 1000.0,
            places=10,
        )
        self.assertEqual(at_25.dg_temperature_c, 25.0)
        self.assertEqual(at_37.dg_temperature_c, 37.0)

    def test_inner_dh_ds_produce_status_rich_zero_crossing(self):
        result = self._calculate()

        self.assertAlmostEqual(result.dh_kcal_mol, DH)
        self.assertAlmostEqual(result.ds_cal_mol_k, DS)
        self.assertAlmostEqual(result.tm_c, EXPECTED_TM_C, places=10)
        self.assertEqual(result.search_range_c, (0.0, 100.0))
        self.assertGreaterEqual(result.numerical_tolerance_c, 0.01)
        self.assertGreater(result.bracket_c[1] - result.bracket_c[0], 0.01)
        self.assertGreaterEqual(result.numerical_tolerance_c, 0.01)
        self.assertLessEqual(result.bracket_c[0], result.tm_c)
        self.assertGreaterEqual(result.bracket_c[1], result.tm_c)
        self.assertEqual(result.path_identity,
                         repr(_hairpin_geometry().identity))

    def test_no_crossing_reports_direction(self):
        cases = (
            (-25.0, -100.0, "below_search_range"),
            (-40.0, -100.0, "above_search_range"),
            (-10.0, 0.0, "no_sign_change"),
        )
        for dh, ds, expected in cases:
            with self.subTest(expected=expected):
                result = self._calculate(dh=dh, ds=ds)
                self.assertEqual(result.status, expected)
                self.assertIsNone(result.tm_c)
                self.assertEqual(result.search_range_c, (0.0, 100.0))
                self.assertIsNotNone(result.dg_kcal_mol)

    def test_missing_and_invalid_inner_totals_fail_closed(self):
        invalid_enthalpy = (
            (None, DS, "missing_enthalpy_parameter"),
            (float("nan"), DS, "calculation_failed"),
            (float("inf"), DS, "calculation_failed"),
            (float("-inf"), DS, "calculation_failed"),
            ("wrong numeric type", DS, "calculation_failed"),
        )
        invalid_entropy = tuple(
            (DH, value, status)
            for value, status in (
                (None, "missing_enthalpy_parameter"),
                (float("nan"), "calculation_failed"),
                (float("inf"), "calculation_failed"),
                (float("-inf"), "calculation_failed"),
                ("wrong numeric type", "calculation_failed"),
            )
        )
        for dh, ds, expected in invalid_enthalpy + invalid_entropy:
            with self.subTest(dh=dh, ds=ds):
                result = self._calculate(dh=dh, ds=ds)
                self.assertEqual(result.status, expected)
                self.assertIsNone(result.dg_kcal_mol)
                self.assertIsNone(result.tm_c)
                self.assertTrue(result.warning)

    def test_thermo_totals_for_another_path_are_rejected(self):
        result = self._calculate(path_identity="different CT geometry")

        self.assertEqual(result.status, "geometry_changed")
        self.assertIsNone(result.dg_kcal_mol)
        self.assertIsNone(result.tm_c)
        self.assertIsNone(result.dh_kcal_mol)
        self.assertIsNone(result.ds_cal_mol_k)


class RNAstructureRunnerThermodynamicsTests(unittest.TestCase):
    HAIRPIN_CT = """9 ENERGY = -99.00 hairpin
1 G 0 2 9 1
2 C 1 3 8 2
3 G 2 4 7 3
4 A 3 5 0 4
5 A 4 6 0 5
6 A 5 7 0 6
7 C 6 8 3 7
8 G 7 9 2 8
9 C 8 0 1 9
"""
    DIMER_CT = """9 ENERGY = -99.00 dimer
1 G 0 2 9 1
2 C 1 3 8 2
3 G 2 4 7 3
4 I 3 5 0 4
5 I 4 6 0 5
6 I 5 7 0 6
7 C 6 8 3 7
8 G 7 9 2 8
9 C 8 0 1 9
"""

    def test_default_25_c_adjusts_hairpin_and_exports_inner_totals(self):
        backend = _NativeFixtureBackend()
        batch = ee._rnastructure_run(
            HAIRPIN_SEQ, None, te.ReactionConditions(), 3,
            session=_native_session(backend))

        self.assertEqual([call[0] for call in backend.calls],
                         ["fold_hairpin", "score_fixed_geometries"])
        self.assertEqual(backend.calls[0][1], 25.0)
        observation = batch.observations[0]
        self.assertNotEqual(observation.dg_kcal_mol, -99.0)
        self.assertAlmostEqual(observation.dg_kcal_mol, _dg_at(25.0))
        self.assertAlmostEqual(observation.tm_c, EXPECTED_TM_C)
        self.assertEqual(observation.tm_status, "crossing_found")
        self.assertAlmostEqual(observation.dh_kcal_mol, DH)
        self.assertAlmostEqual(observation.ds_cal_mol_k, DS)
        payload = observation.to_dict()
        self.assertEqual(payload["dg_temperature_c"], 25.0)
        self.assertEqual(payload["thermo_path_identity"],
                         repr(observation.geometry.identity))

    def test_requested_temperature_propagates_for_dimers(self):
        cond = te.ReactionConditions(dg_temp_c=42.0)
        backend = _NativeFixtureBackend(scores=({
            "dg_37_kcal_mol": _dg_at(37.0), "dh_kcal_mol": DH,
            "dg_requested_kcal_mol": _dg_at(42.0)},))
        batch = ee._rnastructure_run(
            DIMER_A, DIMER_B, cond, 3, session=_native_session(backend))

        self.assertEqual(backend.calls[0], ("fold_duplex", 42.0))
        observation = batch.observations[0]
        self.assertAlmostEqual(observation.dg_kcal_mol, _dg_at(42.0))
        effective_na_M = (
            cond.mv_conc
            + 120.0 * math.sqrt(cond.dv_conc - cond.dntp_conc)
        ) / 1000.0
        corrected_entropy = (
            DS
            + 0.368 * (len(observation.geometry.pairs) - 1)
            * math.log(effective_na_M)
            + 1.9872 * math.log(cond.primer_conc * 1e-9 / 4.0)
        )
        expected_dimer_tm = 1000.0 * DH / corrected_entropy - 273.15
        self.assertAlmostEqual(observation.tm_c, expected_dimer_tm)
        self.assertEqual(observation.tm_method,
                         "rnastructure_primer3_style_dimer_tm")
        self.assertEqual(observation.to_dict()["dg_temperature_c"], 42.0)

    def test_every_suboptimal_keeps_its_own_geometry_thermodynamics(self):
        backend = _NativeFixtureBackend(hairpin_pairs=(
            _hairpin_geometry().pairs, ((0, 8), (1, 7))))
        batch = ee._rnastructure_run(
            HAIRPIN_SEQ, None, te.ReactionConditions(), 3,
            session=_native_session(backend))

        self.assertEqual(len(batch.observations), 2)
        scored = backend.calls[1][2]
        self.assertEqual(len(scored), 2)
        self.assertNotEqual(scored[0], scored[1])
        for observation in batch.observations:
            self.assertEqual(observation.thermo_path_identity,
                             repr(observation.geometry.identity))
            self.assertAlmostEqual(observation.tm_c, EXPECTED_TM_C)

    def test_inner_engine_exception_is_typed_and_never_mislabels_37_c_dg(self):
        backend = _NativeFixtureBackend(
            score_error=RuntimeError("missing dna enthalpy table"))
        batch = ee._rnastructure_run(
            HAIRPIN_SEQ, None, te.ReactionConditions(), 3,
            session=_native_session(backend))

        self.assertTrue(any("enthalpy" in item.lower()
                            for item in batch.diagnostics))
        self.assertEqual(batch.observations, ())
        self.assertEqual(batch.statuses, (("RNAstructure", "engine_error"),))

    def test_exact_geometries_are_rescored_in_one_native_batch(self):
        geometries = (_hairpin_geometry(), _hairpin_geometry())
        backend = _NativeFixtureBackend()
        values = backend.score_fixed_geometries(geometries, 25.0)

        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(backend.calls[0][0], "score_fixed_geometries")
        self.assertEqual(len(values), 2)


class RNAstructurePresentationAndIsolationTests(unittest.TestCase):
    def test_capability_and_manifest_declare_temperature_adjusted_tm_policy(self):
        capability = ee.discover_external_engines()["RNAstructure"]
        self.assertTrue(capability.structure_tm)
        self.assertTrue(
            te._EXTERNAL_METRIC_SUPPORT["hairpin"]["RNAstructure"]["tm"])
        self.assertTrue(
            te._EXTERNAL_METRIC_SUPPORT["dimer"]["RNAstructure"]["tm"])

        with ee.RNAstructureSession() as session:
            manifest = te.create_scientific_manifest(
                [], te.ReactionConditions(), rnastructure_session=session)
        payload = manifest.to_dict()
        metadata = payload["engines"]["RNAstructure"]
        self.assertEqual(payload["conditions"]["dg_temp_c"], 25.0)
        self.assertTrue(metadata["dg_temperature_adjustable"])
        self.assertEqual(metadata["thermo_method"],
                         "fixed_geometry_inner_dh_ds")
        self.assertEqual(metadata["integration"], "in_process_native")
        self.assertEqual(metadata["version"], "6.6")
        self.assertRegex(metadata["native_module_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(
            metadata["dna_table_manifest_sha256"], r"^[0-9a-f]{64}$")

    def test_gui_csv_json_show_adjusted_dg_tm_units_and_provenance(self):
        observation = _rna_observation(_hairpin_geometry())
        geometry = observation.geometry
        result = te.PeerStructure(
            kind="Hairpin", label="Hairpin: fixture #1", found=True,
            geometry=geometry, rank=1,
            discovered_by=("Primer3", "RNAstructure"), dg_p3=-2.0,
            tm_p3_c=40.0, structure="fixture", severity="ok",
            assessment="fixture", engine_observations=(observation,),
        )
        result.external_metric_states = {
            "RNAstructure": {"dg": "available", "tm": "available"},
            "seqfold": {"dg": "not_run", "tm": "not_run"},
        }
        report = te.AnalysisReport(hairpins=[te.InteractionResult(
            "Hairpin", "fixture", geometry.sequence_a, None,
            structures=[result])])

        finding = te.iter_report_findings(report)[0]
        app = object.__new__(gui.MBUprimeStructLabApp)
        app._language_code = "en"
        displayed = dict(zip(
            app._problem_result_columns(), app._finding_row_values(finding)))
        self.assertEqual(displayed["dg"], "-5.28")
        self.assertEqual(displayed["tm"], "60.18")

        rows = list(csv.DictReader(io.StringIO(te.format_csv_report(
            [], report, te.ReactionConditions()))))
        row = next(item for item in rows
                   if item["record_type"] == "structure")
        self.assertEqual(row["dg_rnastructure_kcal_mol"], "-5.28")
        self.assertEqual(row["tm_rnastructure_c"], "60.18")
        serialized = json.loads(row["engine_observations_json"])[0]
        self.assertEqual(serialized["dh_kcal_mol"], DH)
        self.assertEqual(serialized["ds_cal_mol_k"], DS)
        self.assertEqual(serialized["tm_status"], "crossing_found")
        self.assertEqual(serialized["tm_method"],
                         "rnastructure_fixed_geometry_inner_dh_ds")
        self.assertIn("fixed-geometry", serialized["energy_definition"])

    def test_different_geometry_metrics_stay_on_their_peer(self):
        oligo = te.Oligo("control", "GGGGGAAAAACCCCC", "primer", 250.0,
                         ["GGGGGAAAAACCCCC"])
        geometry = ee.hairpin_geometry_from_dot_bracket(
            oligo.seq, "(((.........)))")
        observation = _rna_observation(geometry, dg=-999.0, tm=99.0)
        batch = ee.EngineBatch(
            (observation,), (), (("RNAstructure", "ok"),))

        with patch.object(ee, "run_hairpin_engines", return_value=batch):
            observed = te.analyze_hairpin(
                oligo, te.ReactionConditions())

        matches = [peer for peer in observed.structures
                   if peer.canonical_geometry == geometry]
        self.assertEqual(len(matches), 1)
        self.assertAlmostEqual(matches[0].engine_observations[0].tm_c, 99.0)

    def test_extreme_rnastructure_thermo_changes_exact_peer_severity_and_rank(self):
        cond = te.ReactionConditions()
        oligo = te.Oligo("control", "GGGGGAAAAACCCCC", "primer", 250.0,
                         ["GGGGGAAAAACCCCC"])
        empty = ee.EngineBatch((), (), ())
        with patch.object(ee, "run_hairpin_engines", return_value=empty):
            base_hairpin = te.analyze_hairpin(oligo, cond)
        hp_geometry = base_hairpin.structures[-1].canonical_geometry
        hp_batch = ee.EngineBatch(
            (_rna_observation(hp_geometry, dg=-1e6, tm=1e6),), (),
            (("RNAstructure", "ok"),))
        with patch.object(ee, "run_hairpin_engines", return_value=hp_batch):
            observed_hairpin = te.analyze_hairpin(oligo, cond)

        with patch.object(ee, "run_dimer_engines", return_value=empty):
            base_dimer = te.analyze_self_dimer(oligo, cond)
        dimer_geometry = base_dimer.structures[-1].canonical_geometry
        dimer_batch = ee.EngineBatch(
            (_rna_observation(dimer_geometry, dg=-1e6, tm=1e6),), (),
            (("RNAstructure", "ok"),))
        with patch.object(ee, "run_dimer_engines", return_value=dimer_batch):
            observed_dimer = te.analyze_self_dimer(oligo, cond)

        hp_peer = next(peer for peer in observed_hairpin.structures
                       if peer.canonical_geometry == hp_geometry)
        dimer_peer = next(peer for peer in observed_dimer.structures
                          if peer.canonical_geometry == dimer_geometry)
        self.assertEqual((hp_peer.severity, hp_peer.rank), ("problem", 1))
        self.assertEqual((dimer_peer.severity, dimer_peer.rank), ("problem", 1))
        self.assertIn("RNAstructure", hp_peer.severity_drivers)
        self.assertIn("RNAstructure", dimer_peer.severity_drivers)


class RNAstructureCompatibilityMigrationAdversarialTests(unittest.TestCase):
    """Guards for immutable, prebuilt compatibility tables."""

    def test_bundled_compatibility_tables_are_hash_gated_at_native_boundary(self):
        import rnastructure_native

        backend = rnastructure_native.create_backend()
        identity = backend.runtime_identity()
        self.assertEqual(identity["engine_version"], "6.6")
        self.assertRegex(identity["dna_table_manifest_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            identity["compatibility_patch"],
            "rnastructure_6_6_dna_temperature_tables_v2")

    def test_loops_over_30_keep_direct_dg_but_reject_h_s_tm(self):
        sequence = "G" + ("A" * 31) + "C"
        geometry = ee.CanonicalGeometry(
            "hairpin", sequence, None, ((0, len(sequence) - 1),))

        backend = _NativeFixtureBackend(
            hairpin_pairs=(geometry.pairs,), scores=({
                "dg_37_kcal_mol": -1.0, "dh_kcal_mol": -10.0,
                "dg_requested_kcal_mol": -2.5},))
        batch = ee._rnastructure_run(
            sequence, None, te.ReactionConditions(), 1,
            session=_native_session(backend))

        observation = batch.observations[0]
        self.assertEqual(observation.tm_status, "non_affine")
        self.assertEqual(observation.dg_kcal_mol, -2.5)
        self.assertIsNone(observation.dh_kcal_mol)
        self.assertIsNone(observation.ds_cal_mol_k)
        self.assertIsNone(observation.tm_c)
        self.assertIn("exceeds 30", observation.warning)

    def test_native_rescorer_provenance_has_exact_identity(self):
        backend = _NativeFixtureBackend()
        batch = ee._rnastructure_run(
            HAIRPIN_SEQ, None, te.ReactionConditions(), 1,
            session=_native_session(backend))
        decoded = json.loads(batch.observations[0].provenance)

        self.assertEqual(decoded["integration"], "in_process_native")
        self.assertEqual(decoded["native_identity"]["engine_version"], "6.6")
        self.assertEqual(
            decoded["fixed_geometry_rescorer"]["algorithm"],
            "CalculateFreeEnergy(simple=True)")


class RNAstructureInstallerFailClosedTests(unittest.TestCase):
    def test_optional_cli_installer_is_removed_for_mandatory_native_engine(self):
        root = pathlib.Path(__file__).parents[1]
        self.assertFalse((root / "packaging" /
                          "install_optional_engines.ps1").exists())
        self.assertTrue((root / "native" / "build_native.ps1").is_file())
        self.assertTrue((root / "vendor" / "rnastructure-6.6" /
                         "COPYING").is_file())


if __name__ == "__main__":
    unittest.main()
