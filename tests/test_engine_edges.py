import csv
import io
import unittest

import external_engines as ee
import nn_thermo as nn
import thermo_engine as te


def _peer(kind, label, *, sequence="ACGTACGT", sequence_b=None,
          pairs=((0, 7), (1, 6)), rank=1, dg=-1.0, tm=20.0,
          dg_vienna=None, severity="ok", assessment="ok",
          involves_3prime=False, structure="fixture"):
    geometry_kind = kind.lower()
    if kind == "Hairpin":
        geometry_kind = "hairpin"
    elif kind == "Self-dimer":
        geometry_kind = "self-dimer"
        sequence_b = sequence if sequence_b is None else sequence_b
    else:
        geometry_kind = "hetero-dimer"
        sequence_b = sequence_b or "TGCATGCA"
    geometry = ee.CanonicalGeometry(
        geometry_kind, sequence, sequence_b, tuple(pairs))
    return te.PeerStructure(
        kind=kind, label=f"{kind}: {label} #{rank}", found=True,
        geometry=geometry, structure=structure, rank=rank,
        discovered_by=("Primer3",), dg_p3=dg, tm_p3_c=tm,
        dg_vienna=dg_vienna, severity=severity, assessment=assessment,
        representative_variant=(sequence if sequence_b is None else
                                f"{sequence} / {sequence_b}"),
        involves_3prime=involves_3prime,
    )


def _interaction(kind, label, peers, *, sequence="ACGTACGT", sequence_b=None):
    if kind == "Self-dimer":
        sequence_b = sequence
    return te.InteractionResult(
        kind, label, sequence, sequence_b, structures=list(peers))


class ValidationBoundaryTests(unittest.TestCase):
    def test_build_oligos_rejects_all_empty_inputs(self):
        with self.assertRaisesRegex(te.SequenceError, "at least one oligo"):
            te.build_oligos()

    def test_build_oligos_prefixes_invalid_field_name(self):
        with self.assertRaisesRegex(te.SequenceError, "Reverse primer:"):
            te.build_oligos(fwd="ACGTACGTACGT", rev="ACGTX")

    def test_expand_iupac_allows_exact_variant_cap(self):
        variants = te.expand_iupac("NNNN")

        self.assertEqual(len(variants), te.MAX_VARIANTS)
        self.assertEqual(variants[0], "AAAA")
        self.assertEqual(variants[-1], "TTTT")

    def test_bulk_build_normalizes_u_and_uses_probe_concentration(self):
        cond = te.ReactionConditions(primer_conc=111, probe_conc=222)
        items = te.parse_bulk_oligos(
            "Notes line\nReverse\tacguacguacgu\nT1_probe = AGCCTTGACGATACAGCTAAT"
        )
        oligos = te.build_oligo_list(items, cond)

        self.assertEqual([o.name for o in oligos], ["Reverse", "T1_probe"])
        self.assertEqual([o.seq for o in oligos], ["ACGTACGTACGT", "AGCCTTGACGATACAGCTAAT"])
        self.assertEqual([o.role for o in oligos], ["primer", "probe"])
        self.assertEqual([o.conc_nM for o in oligos], [111, 222])

    def test_bulk_labels_with_pr_or_probe_use_probe_concentration(self):
        cond = te.ReactionConditions(primer_conc=111, probe_conc=222)
        items = [
            ("T1_pr", "AGCCTTGACGATACAGCTAAT"),
            ("Pr2", "TGCAACGGATCCTTAGGCAT"),
            ("T3_probe", "TGCATGCATGCATGCATGCA"),
            ("Primer", "TCCGATGCTGACCTGTGTTA"),
        ]
        oligos = te.build_oligo_list(items, cond)

        self.assertEqual(
            [o.role for o in oligos],
            ["probe", "probe", "probe", "primer"],
        )
        self.assertEqual([o.conc_nM for o in oligos], [222, 222, 222, 111])

    def test_reaction_conditions_reject_invalid_concentrations(self):
        bad_kwargs = [
            {"mv_conc": 0},
            {"dv_conc": 0},
            {"mv_conc": -1},
            {"dv_conc": -1},
            {"dntp_conc": -1},
            {"primer_conc": 0},
            {"probe_conc": -1},
        ]

        for kwargs in bad_kwargs:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(te.SequenceError):
                    te.ReactionConditions(**kwargs)

    def test_reaction_conditions_reject_invalid_thresholds(self):
        bad_kwargs = [
            {"dg_caution": -9, "dg_problem": -6},
            {"dg_caution": -6, "dg_problem": -6},
            {"dg_caution": -6.01, "dg_problem": -6.02},
            {"dv_conc": 0.8, "dntp_conc": 0.8},
            {"dg_temp_c": -274},
        ]

        for kwargs in bad_kwargs:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(te.SequenceError):
                    te.ReactionConditions(**kwargs)


class WarningBoundaryTests(unittest.TestCase):
    def test_dg_thresholds_are_inclusive(self):
        cond = te.ReactionConditions(dg_caution=-6, dg_problem=-9)

        self.assertEqual(te._level(-5.94, cond), ("ok", "Weak"))
        self.assertEqual(te._level(-5.95, cond), ("caution", "Moderately stable"))
        self.assertEqual(te._level(-6.00, cond), ("caution", "Moderately stable"))
        self.assertEqual(te._level(-9.00, cond), ("problem", "Very stable"))

    def test_verdict_handles_no_engine_structure(self):
        severity, assessment, effective_dg, engine = te._verdict(
            None, None, involves_3prime=False, cond=te.ReactionConditions())

        self.assertEqual(severity, "ok")
        self.assertEqual(assessment, "No stable structure detected")
        self.assertIsNone(effective_dg)
        self.assertIsNone(engine)

    def test_hairpin_tm_rule_uses_strict_temperature_boundaries(self):
        for pair_count, caution_tm in [
            (3, 55.0),
            (4, 50.0),
            (5, 45.0),
            (6, 40.0),
        ]:
            with self.subTest(pair_count=pair_count, caution_tm=caution_tm):
                self.assertEqual(
                    te._hairpin_tm_severity(caution_tm, pair_count),
                    "ok",
                )

        self.assertEqual(te._hairpin_tm_severity(60.0, 4), "caution")

        for pair_count, problem_tm in [
            (3, 65.0),
            (4, 60.0),
            (5, 55.0),
            (6, 50.0),
        ]:
            with self.subTest(pair_count=pair_count, problem_tm=problem_tm):
                self.assertNotEqual(
                    te._hairpin_tm_severity(problem_tm, pair_count),
                    "problem",
                )

        self.assertEqual(te._hairpin_tm_severity(60.05, 4), "problem")
        self.assertEqual(te._hairpin_tm_severity(-10.0, 7), "caution")
        self.assertEqual(te._hairpin_tm_severity(None, 7), "caution")

    def test_enumerated_dimer_site_counts_one_base_from_3prime(self):
        cond = te.ReactionConditions()
        structures = nn.enumerate_duplex("AAGG", "CCTT", cond, top_n=10)

        self.assertTrue(
            any(2 in [pair[0] for pair in structure.du_pairs]
                and structure.involves_3prime
                for structure in structures)
        )

    def test_worst_over_ignores_not_found_and_picks_most_negative_dg(self):
        best = te._worst_over(
            [
                (False, 0.0, 0.0, "", "none"),
                (True, -2.0, 40.0, "weak", "weak variant"),
                (True, -5.0, 55.0, "strong", "strong variant"),
            ]
        )

        self.assertEqual(best, (True, -5.0, 55.0, "strong", "strong variant"))


class ReportOutputTests(unittest.TestCase):
    def test_degenerate_report_calls_out_worst_case_semantics(self):
        cond = te.ReactionConditions()
        oligos = te.build_oligos(fwd="ACGTRCGTACGT", cond=cond)
        report = te.analyze(oligos, cond)
        text = te.format_text_report(oligos, report, cond)

        self.assertTrue(report.has_degenerate)
        self.assertIn("degenerate oligos expanded", text)
        self.assertIn("representative of 2 variant combinations", text)
        self.assertIn("ACGTRCGTACGT", text)

    def test_report_summary_counts_flagged_peer_structures(self):
        weak = _peer(
            "Self-dimer", "Primer A", rank=2, dg=-1.0,
            pairs=((0, 7),), structure="SEQ\tACGT\nSTR\tTGCA",
            severity="ok", assessment="peer ok")
        caution = _peer(
            "Self-dimer", "Primer A", rank=1, dg=-7.1,
            dg_vienna=-8.2, tm=44.2, involves_3prime=True,
            assessment="peer caution", severity="caution")
        interaction = _interaction("Self-dimer", "Primer A", [caution, weak])
        report = te.AnalysisReport(self_dimers=[interaction])
        text = te.format_text_report([], report, te.ReactionConditions())

        self.assertEqual(report.problems, [])
        self.assertEqual(report.cautions, [caution])
        self.assertIn("Problems: 0    Cautions: 1", text)
        self.assertIn("dG(P3)=-7.10", text)
        self.assertIn("dG(Vienna)= -8.20", text)
        self.assertIn("Tm= 44.20 C", text)
        self.assertIn("peer caution", text)

    def test_csv_and_tsv_reports_include_tm_and_uniform_peer_structures(self):
        cond = te.ReactionConditions()
        oligo = te.Oligo(
            name="Primer A",
            seq="ACGTACGT",
            role="primer",
            conc_nM=250,
            variants=["ACGTACGT"],
        )
        tm = te.TmResult(
            oligo=oligo,
            tm_mean=61.234,
            tm_min=60.0,
            tm_max=62.0,
            tm_owczarzy_mean=60.5,
            tm_owczarzy_min=59.5,
            tm_owczarzy_max=61.5,
        )
        weak = _peer(
            "Self-dimer", "Primer A", sequence="ACGTACGT", rank=2,
            dg=-1.0, tm=20.0, pairs=((0, 7),),
            structure="SEQ\tACGT\nSTR\tTGCA", severity="ok",
            assessment="peer ok, quoted")
        caution = _peer(
            "Self-dimer", "Primer A", sequence="ACGTACGT", rank=1,
            dg=-7.1, tm=44.2, dg_vienna=-8.2,
            structure="peer\nstructure", severity="caution",
            assessment="peer caution", involves_3prime=True)
        report = te.AnalysisReport(
            tms=[tm], self_dimers=[_interaction(
                "Self-dimer", "Primer A", [caution, weak])])

        csv_rows = list(csv.DictReader(io.StringIO(
            te.format_csv_report([oligo], report, cond))))
        tsv_rows = list(csv.DictReader(io.StringIO(
            te.format_tsv_report([oligo], report, cond)), delimiter="\t"))

        self.assertEqual(len(csv_rows), len(tsv_rows))
        self.assertEqual(csv_rows[0]["record_type"], "analysis_manifest")
        self.assertTrue(csv_rows[0]["manifest_json"])
        first_condition = next(
            row for row in csv_rows if row["record_type"] == "condition")
        self.assertEqual(first_condition["parameter"], "mv_conc")
        self.assertEqual(first_condition["unit"], "mM")

        tm_row = next(r for r in csv_rows if r["record_type"] == "tm")
        self.assertEqual(tm_row["oligo_name"], "Primer A")
        self.assertEqual(tm_row["tm_mean_c"], "61.23")
        self.assertEqual(tm_row["tm_owczarzy_mean_c"], "60.50")
        self.assertEqual(tm_row["sequence"], "ACGTACGT")

        structures = [r for r in csv_rows if r["record_type"] == "structure"]
        self.assertEqual([r["rank"] for r in structures], ["1", "2"])
        first = structures[0]
        self.assertEqual(first["dg_vienna_kcal_mol"], "-8.20")
        self.assertEqual(first["site"], "3' end")
        self.assertEqual(
            first["structure"],
            "5'-ACGTACGT-3'\n   ||      \n3'-TGCATGCA-5'")
        second = structures[1]
        self.assertEqual(second["assessment"], "peer ok, quoted")
        self.assertEqual(
            second["structure"],
            "5'-ACGTACGT-3'\n   |       \n3'-TGCATGCA-5'")
        self.assertIn("structure_tm_vienna_c", second)
        self.assertNotIn("structure_tm_owczarzy_c", second)

    def test_delimited_report_rejects_unknown_delimiter(self):
        with self.assertRaises(te.SequenceError):
            te.format_delimited_report(
                [], te.AnalysisReport(), te.ReactionConditions(), "|")


class ReportNavigationTests(unittest.TestCase):
    def _navigation_report(self):
        hairpin = _peer(
            "Hairpin", "Primer A", dg=-6.2, tm=51.0,
            structure="hairpin", severity="caution",
            assessment="hairpin caution")
        self_problem = _peer(
            "Self-dimer", "Primer A", rank=1, dg=-9.3, tm=45.0,
            structure="self problem", severity="problem",
            assessment="self problem", involves_3prime=True)
        self_ok = _peer(
            "Self-dimer", "Primer A", rank=2, dg=-2.0, tm=20.0,
            pairs=((0, 7),), structure="self ok", severity="ok",
            assessment="self ok")
        hetero_caution = _peer(
            "Hetero-dimer", "Primer A x Primer B", rank=1,
            dg=-7.4, tm=40.0, structure="hetero caution",
            severity="caution", assessment="hetero caution",
            involves_3prime=True)
        hetero_ok = _peer(
            "Hetero-dimer", "Primer A x Primer B", rank=2,
            dg=-5.0, tm=30.0, pairs=((0, 7),), structure="hetero ok",
            severity="ok", assessment="hetero ok")
        report = te.AnalysisReport(
            hairpins=[_interaction("Hairpin", "Primer A", [hairpin])],
            self_dimers=[_interaction(
                "Self-dimer", "Primer A", [self_problem, self_ok])],
            hetero_dimers=[_interaction(
                "Hetero-dimer", "Primer A x Primer B",
                [hetero_caution, hetero_ok], sequence_b="TGCATGCA")],
        )
        oligos = [
            te.Oligo("Primer A", "ACGTACGT", "primer", 250, ["ACGTACGT"]),
            te.Oligo("Primer B", "TGCATGCA", "primer", 250, ["TGCATGCA"]),
        ]
        return oligos, report

    def test_report_findings_sort_problems_first_and_include_all_peers(self):
        _oligos, report = self._navigation_report()
        findings = te.sorted_report_findings(te.iter_report_findings(report))

        self.assertEqual(findings[0].severity, "problem")
        self.assertEqual(findings[0].rank, 1)
        self.assertEqual(findings[0].site, "3' end")
        self.assertEqual(findings[1].severity, "caution")

    def test_report_finding_filters_match_severity_site_and_kind(self):
        _oligos, report = self._navigation_report()
        findings = te.iter_report_findings(report)

        flagged = te.filter_report_findings(findings, severity="Flagged")
        self.assertEqual({f.severity for f in flagged}, {"problem", "caution"})

        three_prime = te.filter_report_findings(findings, site="3' end")
        self.assertTrue(three_prime)
        self.assertTrue(all(f.involves_3prime for f in three_prime))

        heteros = te.filter_report_findings(findings, kind="Hetero-dimer")
        self.assertEqual({f.kind for f in heteros}, {"Hetero-dimer"})

    def test_hetero_matrix_uses_strongest_pair_finding(self):
        oligos, report = self._navigation_report()
        matrix = te.hetero_dimer_matrix(oligos, report)

        diagonal = matrix[("Primer A", "Primer A")].finding
        forward = matrix[("Primer A", "Primer B")].finding
        reverse = matrix[("Primer B", "Primer A")].finding

        self.assertEqual(diagonal.kind, "Self-dimer")
        self.assertEqual(diagonal.rank, 1)
        self.assertEqual(diagonal.severity, "problem")
        self.assertEqual(diagonal.effective_dg, -9.3)
        self.assertEqual(forward.rank, 1)
        self.assertEqual(forward.severity, "caution")
        self.assertEqual(forward.effective_dg, -7.4)
        self.assertEqual(reverse, forward)
