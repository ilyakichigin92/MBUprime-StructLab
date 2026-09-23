# User guide

[English quick start](quick-start.md) · [Быстрый старт на русском](quick-start-ru.md) · [Methods](methods.md) · [CLI](cli.md)

## Start the desktop application

On Windows:

1. Download the `MBUprime-StructLab-<version>-windows-x64-unsigned.zip` application asset (not **Source code (zip)** or **Source code (tar.gz)**) from [Releases](https://github.com/ilyakichigin92/MBUprime-StructLab/releases/latest), verify the adjacent SHA-256 sidecar, and extract the complete ZIP into an application folder.
2. Keep `MBUprime StructLab.exe` and the adjacent `_internal` folder together.
3. Double-click `MBUprime StructLab.exe`.

The packaged application includes Python and all required scientific engines, so no separate installation is needed. Use a short ASCII-only location such as `C:\MBUprimeStructLab`; the bundled Primer3 library may fail when loaded from a path containing non-ASCII characters.

The distributed executable is unsigned. Windows security controls may therefore ask for confirmation or block it according to local policy. Obtain the application only from the project repository and verify its origin before allowing it to run.

For Windows startup diagnostics, run the packaged self-test from PowerShell
and read the log (adjust the executable path to your installation):

```powershell
& "C:\MBUprimeStructLab\MBUprime StructLab.exe" --self-test --self-test-log "$env:TEMP\mbuprime-self-test.log"
```

## What it calculates

For each oligonucleotide, the application reports:

- `Tm SL`: oligonucleotide melting temperature using the SantaLucia salt correction;
- `Tm Owcz`: oligonucleotide melting temperature using the Owczarzy mixed-salt correction;
- predicted hairpins;
- predicted self-dimers.

For every pair of oligonucleotides, it also predicts heterodimers. Each retained structure includes its pairing geometry, number of bonded base pairs (`n`), site classification, free energy (`dG`), available structure melting temperature (`Tm`), and severity.

Reaction settings provide the monovalent-ion, divalent-ion, dNTP, oligonucleotide-concentration, and calculation-temperature inputs used by the supported models.

## Typical workflow

1. Choose **Bulk / multiplex** for a panel or **Single assay** for fixed assay fields.
2. Enter names and sequences, then set primer/probe concentrations in reaction conditions. In bulk input, label tokens `p` or `pr` with optional digits (such as `p1`), or a token containing `probe`, select probe concentration; other labels use primer concentration.
3. Review the parsed input and reaction conditions.
4. Adjust warning thresholds or structure-cluttering controls if required.
5. Select **Analyze**.
6. Review the overview, flagged structures, pairwise matrix, and detailed structure tables.
7. Select a structure to inspect its aligned bases or open its diagram.
8. Export a text, CSV, TSV, or matrix report when needed.

Calculations run in a background worker with cooperative cancellation. During analysis, progress and status continue to update, but result tables are published only after the complete mandatory-engine report is ready. If a run is cancelled or fails, the previous completed report remains available.

Large panels require `n(n-1)/2` heterodimer comparisons, in addition to per-oligonucleotide work. Publishing the completed tables and matrix runs on the GUI thread and can pause interaction for several seconds. Memory use grows with retained results; fast input preview does not imply equally fast full analysis.

The desktop interface opens in English by default and can be switched to Russian from the language selector. The command-line interface keeps English as its default. Exported CSV and TSV field names remain in English for stable downstream processing.

The original two-column workspace is retained: input, parsed preview, conditions and Run are on the left; language, summaries, result tabs and selected-structure details are on the right. On smaller screens, each column scrolls independently. Use its outer vertical scrollbar to reach lower sections and its horizontal scrollbar to reveal controls beyond the column width. Drag the divider to adjust the column widths. Keyboard traversal brings the focused control into view. The editor, result tables and matrix keep their own scrolling; the mouse wheel over labels or buttons scrolls the surrounding column. Starting analysis reveals the Run controls so Cancel is accessible.

### Saved conditions and analyzed runs

Use **Save preset** beside the reaction-condition preset selector to keep a named custom set of every reaction and structure-filter condition. Saved custom presets are available after the application restarts and can be deleted from the same controls. The built-in `qPCR / TaqMan` preset and the `Custom` editing state cannot be overwritten. Presets are local per-user files: `%LOCALAPPDATA%\MBUprime StructLab\condition-presets.json` on Windows, `$XDG_CONFIG_HOME/mbuprime-structlab/condition-presets.json` (or `~/.config/...`) on Linux, and `~/Library/Application Support/MBUprime StructLab/condition-presets.json` on macOS.

After a run completes, choose **Export -> Analyzed run archive** to save its oligonucleotides, conditions, additional-analysis controls, complete results, engine observations, coverage records, diagnostics, and scientific manifest. Version 2.4.3 writes a compressed `.mbusl-run` schema-2 archive. It is a deterministic ZIP64 container with DEFLATE-compressed control JSON and line-streamed JSONL records, so export and validation do not require a second in-memory copy of the complete JSON graph. Existing schema-1 JSON analyzed runs remain importable, but new exports use schema 2 only.

**Import analyzed run** validates the archive and shows a review dialog with its inputs, conditions, coverage, source version, scientific policy, and analysis ID before anything changes. Confirming **Import** restores that completed run without running Primer3, ViennaRNA, RNAstructure, seqfold, or LocalEnumerator again. Archives using the current or an explicitly recognized historical policy are restored exactly; the preview identifies historical results and explains that they are not recalculated under the current policy. Damaged files, unknown policies, and incompatible archive or scientific schemas are rejected. A different application version is allowed only when the envelope and saved manifest agree.

Schema-2 imports are bounded to 5,000,000 aggregate JSONL records, 256 MiB compressed and 512 MiB expanded data, 16 MiB per JSONL record, 8 MiB per control member, JSON depth 64, 250,000 JSON values per record or document, and a 250:1 compression ratio per member and for the aggregate archive. Limits are inclusive. Streaming avoids materializing the full serialized graph, but the application still reconstructs the complete typed report for interactive use; exceptionally large accepted runs therefore still require memory proportional to the restored scientific result.

Both features read and write only files on the local computer; they do not upload sequences or results. An analyzed-run archive contains assay inputs and results, so handle it according to the laboratory's data policy. Member and archive SHA-256 checksums detect accidental modification; they are integrity checks, not signatures or proof of who created the file.

## Reading the results

Each structure is classified as **OK**, **CAUTION**, or **PROBLEM**.

- More-negative `dG` means a more stable predicted structure.
- Hairpins can also be raised to a warning level by their stem length and highest validated structure `Tm`.
- A **3' end** site indicates that terminal or near-terminal bases participate in a dimer and may deserve closer attention.
- `n` is the number of bonded nucleotide pairs in the displayed structure.

The default dG thresholds are:

| Classification | Threshold |
|---|---:|
| Caution | `dG <= -7 kcal/mol` |
| Problem | `dG <= -10 kcal/mol` |

Hairpins have additional stem-length/Tm rules shown in the application's **Rules and engines** dialog. These classifications are screening aids rather than guarantees of assay performance; final candidates should be assessed in the context of the complete assay and laboratory conditions.

The main result areas are:

- **Melting temperatures** for oligonucleotide-level `Tm SL` and `Tm Owcz` values;
- **Flagged structures** for the highest-priority findings;
- **Matrix** for self-dimer and heterodimer risk across a multiplex panel;
- **Structures** for hairpins, self-dimers, heterodimers, engine provenance, and diagrams.

## Exports

Available formats include:

- a human-readable text report;
- a complete CSV or TSV report;
- a CSV containing flagged structures;
- a CSV representation of the dimer matrix.

Machine-readable exports preserve engine-specific measurements, unavailable-state explanations, exact geometry, severity, and analysis coverage. Numeric fields remain numeric or blank rather than mixing values with status text.
