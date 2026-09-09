# Small synthetic panel

Two invented, complementary 12-base DNA oligonucleotides demonstrate hairpins,
self-dimers and heterodimers. They are not primers for a biological target or a
validated assay. No private or experimental sequences are used.

| Name | Role | Sequence |
|---|---|---|
| F_demo | primer | GCGCAAAAGCGC |
| R_demo | primer | GCGCTTTTGCGC |

## Desktop

Follow the [Windows download instructions](../../README.md), choose **Bulk /
multiplex**, and paste all of [bulk.txt](bulk.txt). This file uses the GUI's
two-column label/sequence format; the CLI uses the separate [panel.tsv](panel.tsv)
with its exact `name`, `role`, `sequence` header. Both files describe the same panel.
Select the default **qPCR / TaqMan** conditions and **Analyze**.

The [README screenshot](../../docs/images/analysis-example.png) is an unedited
capture of the actual English v2.4.3 source GUI using these sequences and default
conditions. It shows flagged structures and an ASCII hairpin detail. The capture
is not a frozen-executable or clean-host qualification result.

## Command line

After [source setup](../../docs/development.md), run from the repository root:

```powershell
python -m mbuprime_structlab analyze examples/small-panel/panel.tsv --format tsv --progress text --max-workers 1 --output example-result.tsv
```

This command was executed on 2026-09-09 using the release-matching 2.4.3 scientific
source and native extension on Windows 10 build 19045, CPython 3.12.14 x64. It
completed all seven progress steps and exited `0`. [result.tsv](result.tsv) is the
actual completed export, including the scientific manifest and engine-specific
values. The GUI and CLI use the same defaults here; `--max-workers 1` limits
scheduling concurrency and does not change the reaction conditions.

Conditions: 50 mM monovalent ions, 3 mM divalent ions, 0.8 mM dNTP, 300 nM primer
and 150 nM probe concentration, dG temperature 25 °C; caution/problem thresholds
−7/−10 kcal/mol. Additional analysis is enabled with budget 500; both dimer gap
limits are 0 and the near-duplicate bond-difference threshold is 4.

## Observed result

| Quantity | Actual value |
|---|---|
| Oligonucleotides | 2, one concrete variant each |
| Tm SL / Tm Owcz, both oligos | 53.78 °C / 52.78 °C |
| Retained hairpins | 5 |
| Retained self-dimers | 7 |
| Retained heterodimers | 4 |
| Total structures | 16: 6 problem, 7 caution, 3 OK |
| Concrete-context coverage | Complete: 5/5 contexts; seqfold dimers unsupported |
| First heterodimer dG | Primer3 −18.51, ViennaRNA −10.40, RNAstructure −20.80 kcal/mol |

Scientific manifest schema: `7`; policy:
`2026-09-08-context-retention-and-duplex-offsets-1`.
Engines: primer3-py 2.3.0 / libprimer3 2.6.1, ViennaRNA 2.7.2,
RNAstructure 6.6, seqfold 0.10.2.

Compare the scientific rows and conditions rather than demanding a byte-identical
export from a later checkout: source/native identities in the manifest describe
the generating build. A missing engine-specific value is not zero; see the
corresponding state and observation fields. Hairpin severity also uses stem/Tm
rules, so it need not follow the dG thresholds alone. These are model predictions;
the example demonstrates execution and interpretation, not biological validation
or a performance benchmark.
