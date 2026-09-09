"""Contract and regression tests for mandatory peer folding engines.

These tests deliberately use frozen output strings and patched runners.  The
normal test suite must never require a third-party executable or network.
"""

from __future__ import annotations

import csv
import io
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import external_engines as ee
import primer_tool_gui as gui
import thermo_engine as te


HAIRPIN_SEQ = "GCGAAACGC"
OTHER_SEQ = "CGCTTTCGC"


def _observation(*, engine: str, geometry: ee.CanonicalGeometry,
                 dg: float | None = -4.25, tm: float | None = 42.5,
                 search_mode: str = "mfe") -> ee.EngineObservation:
    return ee.EngineObservation(
        engine=engine, engine_version="test-1", status="ok",
        interaction_type=geometry.kind, search_mode=search_mode,
        geometry=geometry, dg_kcal_mol=dg, tm_c=tm,
        energy_definition=f"{engine} test energy",
        tm_definition=f"{engine} test Tm" if tm is not None else "unsupported",
        structure_scope="frozen fixture", structure_text="fixture structure",
        warning="", provenance="fixture:test_external_engines",
        tm_status="crossing_found" if tm is not None else "not_calculated",
        tm_method="fixed_geometry" if tm is not None else "not_calculated",
    )


def _batch(*observations: ee.EngineObservation) -> ee.EngineBatch:
    return ee.EngineBatch(
        observations=tuple(observations), diagnostics=(),
        statuses=tuple((engine, "ok") for engine in sorted(
            {o.engine for o in observations})),
    )


class CanonicalGeometryContractTests(unittest.TestCase):
    def test_hairpin_identity_is_a_pair_set_not_display_text(self):
        first = ee.CanonicalGeometry(
            "hairpin", HAIRPIN_SEQ, None, ((2, 6), (0, 8), (1, 7)))
        reordered = ee.CanonicalGeometry(
            "hairpin", HAIRPIN_SEQ, None, ((8, 0), (7, 1), (6, 2)))

        self.assertEqual(first, reordered)
        self.assertEqual(hash(first), hash(reordered))

    def test_dimer_identity_is_invariant_to_strand_swap(self):
        forward = ee.CanonicalGeometry(
            "heterodimer", HAIRPIN_SEQ, OTHER_SEQ,
            ((0, 8), (1, 7), (2, 6)))
        swapped = ee.CanonicalGeometry(
            "heterodimer", OTHER_SEQ, HAIRPIN_SEQ,
            ((8, 0), (7, 1), (6, 2)))

        self.assertEqual(forward, swapped)
        self.assertEqual(forward.identity, swapped.identity)
        self.assertEqual(forward.sequence_a, HAIRPIN_SEQ)
        self.assertEqual(forward.sequence_b, OTHER_SEQ)
        self.assertEqual(swapped.sequence_a, OTHER_SEQ)
        self.assertEqual(swapped.sequence_b, HAIRPIN_SEQ)

    def test_self_dimer_keeps_directional_inter_copy_bonds_distinct(self):
        sequence = "GAAAAAAAAAAAAAC"
        geometry = ee.CanonicalGeometry(
            "self-dimer", sequence, sequence, ((0, 14), (14, 0)))

        self.assertEqual(set(geometry.pairs), {(0, 14), (14, 0)})

    def test_hairpin_rejects_cross_role_endpoint_reuse_but_dimer_allows_it(self):
        with self.assertRaisesRegex(ValueError, "one base"):
            ee.CanonicalGeometry(
                "hairpin", "AAAAAA", None, ((0, 2), (2, 5)))

        dimer = ee.CanonicalGeometry(
            "hetero-dimer", "AAAAAA", "TTTTTT", ((0, 2), (2, 5)))
        self.assertEqual(dimer.pairs, ((0, 2), (2, 5)))

    def test_geometry_constructors_reject_unbalanced_or_impossible_pairs(self):
        geometry = ee.hairpin_geometry_from_dot_bracket(
            HAIRPIN_SEQ, "(((...)))")
        self.assertEqual(geometry.pairs, ((0, 8), (1, 7), (2, 6)))
        with self.assertRaises(ValueError):
            ee.hairpin_geometry_from_dot_bracket(HAIRPIN_SEQ, "((.......")



class EngineCapabilityContractTests(unittest.TestCase):
    def test_seqfold_capability_is_hairpin_mfe_only(self):
        empty = ee.EngineBatch((), (), ())
        with patch.object(ee, "_rnastructure_run", return_value=empty), \
                patch.object(ee, "_seqfold_hairpin") as seqfold:
            dimer_batch = ee.run_dimer_engines(
                HAIRPIN_SEQ, OTHER_SEQ, te.ReactionConditions())

        seqfold.assert_not_called()
        self.assertEqual(dimer_batch.observations, ())


class ExactBatchCacheCancellationTests(unittest.TestCase):
    @staticmethod
    def _result(status: str) -> ee.EngineBatch:
        return ee.EngineBatch((), (), (("RNAstructure", status),))

    def test_cancelled_waiter_returns_without_cancelling_owner(self):
        owner_started = threading.Event()
        release_owner = threading.Event()
        waiter_polled = threading.Event()
        waiter_cancelled = threading.Event()
        owner_cancelled = threading.Event()
        owner_results = []
        waiter_errors = []

        class Session:
            fingerprint = ("distinct-cancel-events",)

        class ObservableCancel:
            def is_set(self):
                waiter_polled.set()
                return waiter_cancelled.is_set()

        def owner_compute(*_args, **kwargs):
            self.assertIs(kwargs["cancel_event"], owner_cancelled)
            owner_started.set()
            self.assertTrue(release_owner.wait(2.0))
            return self._result("ok")

        def run(cancel_event):
            return ee.run_dimer_engines(
                HAIRPIN_SEQ, OTHER_SEQ, te.ReactionConditions(),
                session=Session(), cancel_event=cancel_event)

        ee.clear_external_result_cache()
        with patch.object(ee, "_rnastructure_run", side_effect=owner_compute) as runner:
            owner = threading.Thread(target=lambda: owner_results.append(
                run(owner_cancelled)), daemon=True)
            owner.start()
            self.assertTrue(owner_started.wait(1.0))
            waiter = threading.Thread(target=lambda: self._capture_error(
                waiter_errors, run, ObservableCancel()), daemon=True)
            waiter.start()
            self.assertTrue(waiter_polled.wait(1.0))

            waiter_cancelled.set()
            waiter.join(1.0)
            self.assertFalse(
                waiter.is_alive(), "cancelled waiter did not return promptly")
            self.assertIsInstance(waiter_errors[0], ee.ExternalEngineCancelled)
            self.assertTrue(owner.is_alive(), "waiter cancellation leaked to owner")

            release_owner.set()
            owner.join(1.0)
            cached = run(threading.Event())

        self.assertEqual(owner_results[0].statuses, (("RNAstructure", "ok"),))
        self.assertIs(cached, owner_results[0])
        self.assertEqual(runner.call_count, 1)

    @staticmethod
    def _capture_error(errors, run, cancel_event):
        try:
            run(cancel_event)
        except Exception as exc:
            errors.append(exc)

    def test_cancelled_and_failed_owner_results_remain_retryable(self):
        cache = ee._ExactBatchCache()
        attempts = []

        def cancelled():
            attempts.append("cancelled")
            raise ee.ExternalEngineCancelled("cancelled owner")

        with self.assertRaises(ee.ExternalEngineCancelled):
            cache.get_or_compute(("cancel",), cancelled)
        recovered = cache.get_or_compute(
            ("cancel",), lambda: (attempts.append("recovered")
                                   or self._result("ok")))

        failed = cache.get_or_compute(
            ("failure",), lambda: self._result("engine_error"))
        retried = cache.get_or_compute(
            ("failure",), lambda: self._result("ok"))

        self.assertEqual(attempts, ["cancelled", "recovered"])
        self.assertEqual(recovered.statuses, (("RNAstructure", "ok"),))
        self.assertEqual(failed.statuses, (("RNAstructure", "engine_error"),))
        self.assertEqual(retried.statuses, (("RNAstructure", "ok"),))


class MergeAndDecisionIsolationTests(unittest.TestCase):
    @staticmethod
    def _oligo() -> te.Oligo:
        return te.Oligo("Primer A", HAIRPIN_SEQ, "primer", 250,
                        [HAIRPIN_SEQ])

    def test_same_geometry_attaches_metrics_without_duplicate_structure(self):
        oligo = self._oligo()
        cond = te.ReactionConditions()
        with patch("external_engines.run_hairpin_engines",
                   return_value=ee.EngineBatch((), (), ())):
            baseline = te.analyze_hairpin(oligo, cond)
        geometry = baseline.structures[0].canonical_geometry
        batch = _batch(
            _observation(engine="RNAstructure", geometry=geometry),
            _observation(engine="seqfold", geometry=geometry, dg=-2.0,
                         tm=None),
        )

        with patch("external_engines.run_hairpin_engines",
                   return_value=batch):
            result = te.analyze_hairpin(oligo, cond)

        self.assertEqual(len(result.structures), len(baseline.structures))
        merged = next(peer for peer in result.structures
                      if peer.canonical_geometry == geometry)
        self.assertEqual(
            {o.engine for o in merged.engine_observations},
            {"RNAstructure", "seqfold"},
        )

    def test_non_near_external_geometry_adds_one_peer(self):
        oligo = self._oligo()
        cond = te.ReactionConditions()
        geometry = ee.hairpin_geometry_from_dot_bracket(
            HAIRPIN_SEQ, "((...))..")
        batch = _batch(_observation(
            engine="RNAstructure", geometry=geometry, search_mode="suboptimal"))

        with patch("external_engines.run_hairpin_engines",
                   return_value=batch):
            result = te.analyze_hairpin(oligo, cond)

        matching = [
            peer for peer in result.structures
            if peer.canonical_geometry == geometry
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].engine_observations[0].engine,
                         "RNAstructure")

    def test_extreme_external_numbers_participate_in_severity_and_ordering(self):
        oligo = self._oligo()
        cond = te.ReactionConditions()
        with patch("external_engines.run_hairpin_engines",
                   return_value=ee.EngineBatch((), (), ())):
            baseline = te.analyze_hairpin(oligo, cond)
        geometry = baseline.structures[-1].canonical_geometry
        extreme = _batch(_observation(
            engine="RNAstructure", geometry=geometry,
            dg=-1_000_000.0, tm=1_000_000.0))

        with patch("external_engines.run_hairpin_engines",
                   return_value=extreme):
            observed = te.analyze_hairpin(oligo, cond)

        merged = next(peer for peer in observed.structures
                      if peer.canonical_geometry == geometry)
        self.assertEqual(merged.severity, "problem")
        self.assertEqual(observed.structures[0].canonical_geometry, geometry)
        self.assertIn("RNAstructure", merged.severity_drivers)

    def test_mandatory_engine_exception_fails_closed(self):
        oligo = self._oligo()
        with patch("external_engines.run_hairpin_engines",
                   side_effect=RuntimeError("fixture crash")):
            with self.assertRaisesRegex(
                    ee.RequiredScientificEngineError, "fixture crash"):
                te.analyze_hairpin(oligo, te.ReactionConditions())

    def test_external_geometry_is_retained_and_can_rank_ahead_of_local_peers(self):
        sequence = "GGGGGAAAAACCCCC"
        oligo = te.Oligo("Primer A", sequence, "primer", 250, [sequence])
        cond = te.ReactionConditions()
        empty = ee.EngineBatch((), (), ())
        with patch("external_engines.run_hairpin_engines", return_value=empty):
            baseline = te.analyze_hairpin(oligo, cond)
        external_geometry = ee.hairpin_geometry_from_dot_bracket(
            sequence, "(((.........)))")
        batch = _batch(_observation(
            engine="seqfold", geometry=external_geometry, dg=-1000.0,
            tm=1000.0))
        with patch("external_engines.run_hairpin_engines", return_value=batch):
            observed = te.analyze_hairpin(oligo, cond)

        observed_identities = {
            peer.canonical_geometry.identity for peer in observed.structures}
        non_near_baseline = {
            peer.canonical_geometry.identity
            for peer in baseline.structures
            if not peer.involves_3prime
            or len(set(peer.canonical_geometry.pairs)
                   ^ set(external_geometry.pairs)) >= 4
        }
        self.assertTrue(non_near_baseline <= observed_identities)
        external = next(peer for peer in observed.structures
                        if peer.canonical_geometry == external_geometry)
        self.assertEqual(external.severity, "problem")
        self.assertEqual(external.rank, 1)
        self.assertEqual(external.discovered_by, ("seqfold",))
        self.assertEqual(external.engine_observations[0].dg_kcal_mol, -1000.0)
        self.assertEqual(external.engine_observations[0].tm_c, 1000.0)


class ReportingContractTests(unittest.TestCase):
    def _report(self):
        geometry = ee.hairpin_geometry_from_dot_bracket(
            HAIRPIN_SEQ, "(((...)))")
        result = te.PeerStructure(
            kind="Hairpin", label="Hairpin: Primer A #1", found=True,
            geometry=geometry, rank=1, discovered_by=(
                "Primer3", "RNAstructure", "seqfold"), dg_p3=-5.0,
            tm_p3_c=44.0, structure="fixture", severity="caution",
            assessment="baseline", dg_vienna=-4.8,
            engine_observations=(
                _observation(engine="RNAstructure", geometry=geometry,
                             dg=-3.1, tm=None),
                _observation(engine="seqfold", geometry=geometry,
                             dg=-3.4, tm=None),
            ),
        )
        return te.AnalysisReport(hairpins=[te.InteractionResult(
            "Hairpin", "Primer A", HAIRPIN_SEQ, None,
            structures=[result])])

    def test_delimited_export_has_separate_columns_and_full_observations_json(self):
        report = self._report()
        rows = list(csv.DictReader(io.StringIO(te.format_csv_report(
            [], report, te.ReactionConditions()))))
        row = next(r for r in rows if r["record_type"] == "structure")

        self.assertEqual(row["dg_rnastructure_kcal_mol"], "-3.10")
        self.assertEqual(row["dg_seqfold_kcal_mol"], "-3.40")
        self.assertEqual(row["tm_rnastructure_c"], "")
        self.assertEqual(row["tm_seqfold_c"], "")
        full = json.loads(row["engine_observations_json"])
        self.assertEqual({item["engine"] for item in full}, {
            "RNAstructure", "seqfold"})

    def test_gui_row_aggregates_risk_metrics_and_omits_unsupported_engines(self):
        finding = te.iter_report_findings(self._report())[0]
        app = object.__new__(gui.MBUprimeStructLabApp)
        app._language_code = "en"

        values = app._finding_row_values(finding)
        columns = app._problem_result_columns()
        by_column = dict(zip(columns, values))

        self.assertEqual(by_column["dg"], "-5.00")
        self.assertEqual(by_column["tm"], "44.00")
        self.assertNotIn("dg_rnastructure", columns)
        self.assertNotIn("dg_seqfold", columns)
        self.assertNotIn("tm_rnastructure", columns)
        self.assertNotIn("tm_seqfold", columns)


if __name__ == "__main__":
    unittest.main()
