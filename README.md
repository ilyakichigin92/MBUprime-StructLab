# MBUprime StructLab

MBUprime StructLab screens PCR and qPCR oligonucleotides for dimers and hairpins. The Windows distribution provides the desktop application and headless CLI source. Ubuntu packaging now produces a native desktop application and separate headless CLI in one onedir tarball, while macOS remains CLI-only; both non-Windows targets remain unverified until their native jobs complete.

The application accepts a single assay or a multiplex panel. Analysis runs locally on the computer; oligonucleotide sequences and results are not uploaded to an external service.

## Start the desktop application

On Windows:

1. Extract the complete versioned Windows ZIP and open its application folder. In a development checkout, the new candidate is under `dist\MBUprime StructLab`; the preserved `release` folder is an older 2.4.0 snapshot.
2. Keep `MBUprime StructLab.exe` and the adjacent `_internal` folder together.
3. Double-click `MBUprime StructLab.exe`.

The packaged application includes Python and all required scientific engines, so no separate installation is needed. Use a short ASCII-only location such as `C:\MBUprimeStructLab`; the bundled Primer3 library may fail when loaded from a path containing non-ASCII characters.

The distributed executable is unsigned. Windows security controls may therefore ask for confirmation or block it according to local policy. Obtain the application only from the project repository and verify its origin before allowing it to run.

For Windows startup diagnostics, run the packaged self-test from PowerShell
and read the log (adjust the executable path to your installation):

```powershell
& "C:\MBUprimeStructLab\MBUprime StructLab.exe" --self-test --self-test-log "$env:TEMP\mbuprime-self-test.log"
```

On Ubuntu, after the native workflow is green and its artifact has been published, verify the adjacent `.sha256` file, extract the complete tarball, and keep both launchers beside the shared `_internal` directory. Start the desktop interface with `./MBUprime\ StructLab`; run the packaged CLI as `./mbuprime-structlab analyze INPUT [options]`. This is an onedir package: moving either launcher away from `_internal` breaks it. No Python installation is required to run that packaged artifact. The Ubuntu package is not yet a verified release artifact and must not be advertised as one until the native job records evidence and a final checksum.

### Install from source on Ubuntu

Ubuntu 24.04 x86-64 can also build a private, versioned installation from a verified source checkout. Install 64-bit CPython 3.12 with `venv`, development-header, and Tk support, plus `g++`, `make`, and Fontconfig. From the repository root, run:

```sh
make install
make verify-install
```

For a normal user, `make install` defaults to `$HOME/.local`; when run as root it defaults to `/usr/local`. The installer never runs `sudo` and never installs packages into the system Python. It creates an isolated environment under `$PREFIX/lib/mbuprime-structlab/2.4.3/venv`, builds the pinned in-process RNAstructure extension locally, validates all mandatory engines, performs a display-free live CLI analysis, and only then atomically updates the relative `current` link. The commands are `$PREFIX/bin/mbuprime-structlab` and `$PREFIX/bin/mbuprime-structlab-gui`; a desktop entry is installed under `$PREFIX/share/applications`.

Use an absolute custom prefix with `make install PREFIX=/absolute/path`. The filesystem root is deliberately rejected as a prefix. Packaging roots are supported with an empty or absolute non-root `DESTDIR`, for example `make install PREFIX=/usr/local DESTDIR=/tmp/package-root`. `DESTDIR` is not embedded in installed launchers. For an offline build, first collect every artifact named by `requirements-targets/ubuntu-source-build.txt` and `requirements-targets/ubuntu-24.04-x86_64.txt`, then pass the absolute directory as `WHEELHOUSE=/absolute/wheelhouse`.

`make print-install-layout` shows every destination. `make verify-gui` performs the real Tk self-test and therefore needs an active display or `xvfb-run -a make verify-gui`. `make uninstall` removes only files bearing the matching installation markers; it refuses to recursively remove unmarked paths. A failed build or same-version reinstall leaves the previously activated version and launchers intact. After an interrupted replacement, rerun `make install`; recovery recognizes matching transaction markers and restores the prior version or removes a completed transaction remnant before retrying. This does not guarantee recovery after power loss or filesystem corruption.

The source-install route remains unverified until the native Ubuntu workflow completes successfully; the existing PyInstaller onedir tarball workflow is unchanged.

## Headless command line

The console entry point and module form are equivalent:

```text
mbuprime-structlab analyze INPUT [options]
python -m mbuprime_structlab analyze INPUT [options]
```

`INPUT` is strict UTF-8 TSV. Its first row must be exactly `name<TAB>role<TAB>sequence`; every nonblank later row has exactly those three fields, names are unique, and `role` is `primer` or `probe`. Blank lines are ignored. Use `-` as `INPUT` for stdin and as `--output -` for stdout.

`mbuprime-structlab --version` prints the installed version. The `analyze`
options are:

| Option | Domain and default | Effect |
|---|---|---|
| `INPUT` | Required UTF-8 TSV path or `-` | Reads the oligonucleotide panel; `-` reads stdin. |
| `-o`, `--output PATH` | Path or `-`; default `-` | Writes the completed report atomically, or writes to stdout for `-`. |
| `--format` | `auto`, `text`, `csv`, or `tsv`; default `text` | Selects report format. `auto` uses the output extension and otherwise falls back to text. |
| `--progress` | `auto`, `text`, `json`, or `none`; default `auto` | Selects the stderr-only progress protocol. `auto` uses text on an interactive terminal and no progress otherwise. |
| `--language` | `en` or `ru`; default `en` | Selects human-readable progress and errors; report field names remain English. |
| `--mv-conc MM` | Finite `> 0`; default `50` mM | Sets monovalent-cation concentration. |
| `--dv-conc MM` | Finite `> 0`; default `3` mM | Sets divalent-cation concentration; it must exceed dNTP concentration so free Mg2+ remains positive. |
| `--dntp-conc MM` | Finite `>= 0`; default `0.8` mM | Sets total dNTP concentration; it must remain below divalent concentration. |
| `--primer-conc NM` | Finite `> 0`; default `300` nM | Sets primer strand concentration. |
| `--probe-conc NM` | Finite `> 0`; default `150` nM | Sets probe strand concentration. |
| `--dg-temp-c C` | Finite `> -273.15`; default `25` C | Sets the temperature for structure-dG evaluation. |
| `--dg-caution KCAL_MOL` | Finite; default `-7` kcal/mol | Flags a structure when its minimum supported dG is at or below this caution threshold. |
| `--dg-problem KCAL_MOL` | Finite; default `-10` kcal/mol | Sets the problem threshold; it must be more negative than the caution threshold and distinct at 0.1 kcal/mol decision resolution. |
| `--near-duplicate-bond-difference BONDS` | Integer `>= 0`; default `4` | Suppresses a directly dominated peer when the symmetric bond difference is less than this value; `0` disables suppression. |
| `--dimer-max-consecutive-gaps BASES` | Integer `>= 0`; default `0` | Limits the longest retained run of internal dimer gap bases. |
| `--dimer-max-total-gaps BASES` | Integer `>= 0`; default `0` | Limits the total retained internal dimer gap bases. |
| `--additional-analysis`, `--no-additional-analysis` | Boolean; enabled by default | Enables or disables budgeted analysis of additional concrete degenerate contexts. |
| `--additional-analysis-size COUNT` | Integer `>= 0`; default `500` | Sets the additional-context budget; `0` retains representative analysis only. |
| `--max-workers COUNT` | Integer `>= 1`; default `MBUPRIME_RNASTRUCTURE_WORKERS` or `8` | Requests native-search concurrency; the effective value is capped at eight. |

Reaction-condition decimal values accept either a decimal point or decimal
comma. Integer controls do not accept decimal separators.

Reports are emitted only after all mandatory engines finish. Text, CSV, and TSV output is canonical UTF-8 with LF line endings; file output uses atomic replacement. Report data goes only to stdout/the selected file, while progress and errors go only to stderr. `auto` progress is text on an interactive stderr and disabled otherwise. `Ctrl+C` requests cooperative cancellation and exits `130` without publishing a partial file.

Stable exits are `0` success, `2` command or reaction-condition validation, `3` input/preflight/integrity/unsupported target, `4` analysis runtime, `5` output write, and `130` cancellation. English is the default; `--language ru` selects Russian human-readable status and errors. Machine-readable report field names remain English.

Example:

```text
printf 'name\trole\tsequence\nF1\tprimer\tACGTACGTACGT\n' | mbuprime-structlab analyze - --format tsv --progress json
```

## Platform support

| Target | Delivery | Status |
|---|---|---|
| Windows 10 22H2 x86-64, CPython 3.12 | GUI onedir candidate; source CLI | Tested on build 19045; exact candidate evidence and qualification limits are recorded separately |
| Windows 11 x86-64, CPython 3.12 | Intended GUI onedir support target | Clean Windows 11 qualification pending; Windows 10 evidence does not verify Windows 11 |
| Ubuntu 24.04 x86-64, CPython 3.12 | Native PyInstaller **onedir** tarball, or transactional `make install` into a private versioned environment | Implementation-ready; native source-install, Xvfb GUI, DISPLAY-free CLI, and artifact verification pending |
| macOS 14 arm64, CPython 3.12 | Tagged-source virtual-environment CLI only | Implementation-ready; target-host build/test pending |

macOS has no GUI, `.app`, DMG, standalone binary, signing, or notarization route. A source-installed command is not a notarized application. All targets fail closed unless exact seqfold and target-built RNAstructure identities validate; there is no optional-engine mode.

The Windows candidate is unsigned development software. File hashes identify bytes but do not authenticate its publisher. Consult [the release contract](packaging/RELEASE.md) for candidate identity, clean-host qualification and publication conditions.

For macOS, install CPython 3.12 and the Xcode Command Line Tools (`xcode-select --install`), then check out a signed or otherwise independently verified release tag and create a virtual environment from that source tree. Confirm that `xcrun --find clang` succeeds. Inside the environment, install `requirements-targets/macos-primer3-build.txt` with `--require-hashes`, install `requirements-targets/macos-14-arm64.txt` with `--require-hashes --no-build-isolation`, and finally install this project with `--no-deps --no-build-isolation`. `packaging/verify_macos_cli.sh` performs that exact sequence, builds and validates RNAstructure, checks the headless import boundary, and runs a live four-engine analysis. Do not use an arbitrary moving branch or a plain dependency-resolving `pipx install .` command as the verified route.

## What it calculates

For each oligonucleotide, the application reports:

- `Tm SL`: oligonucleotide melting temperature using the SantaLucia salt correction;
- `Tm Owcz`: oligonucleotide melting temperature using the Owczarzy mixed-salt correction;
- predicted hairpins;
- predicted self-dimers.

For every pair of oligonucleotides, it also predicts heterodimers. Each retained structure includes its pairing geometry, number of bonded base pairs (`n`), site classification, free energy (`dG`), available structure melting temperature (`Tm`), and severity.

Reaction settings provide the monovalent-ion, divalent-ion, dNTP, oligonucleotide-concentration, and calculation-temperature inputs used by the supported models.

## Scientific engines

Four thermodynamic engines are mandatory and run inside the application process:

- **Primer3** uses its two-state thermodynamic model.
- **ViennaRNA 2.7.2** uses a loop-based DNA model. For dimers, the reported binding energy separates the intermolecular contribution from the individual strand energies.
- **RNAstructure 6.6** searches for hairpins and dimers and evaluates retained fixed structures.
- **seqfold 0.10.2** evaluates hairpins and fixed hairpin thermodynamics. It does not support dimers.

**LocalEnumerator** is a candidate generator, not a thermodynamic engine. It proposes additional pairing geometries, which the applicable thermodynamic engines then evaluate.

Results from matching geometries are merged. The table-level `dG` is the lowest supported finite value across applicable engines, while hairpin `Tm` uses the highest validated finite value. Engine-specific measurements and provenance remain available in detailed reports and exports.

Gap limits remove unsupported dimer geometries before near-duplicate structures are collapsed. A row labelled **Gap-policy derived** represents a geometry produced by applying those limits to an engine candidate and then recalculating the retained exact structure.

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

The desktop interface opens in Russian by default and can be switched to English from the language selector. The command-line interface keeps English as its default. Exported CSV and TSV field names remain in English for stable downstream processing.

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

## Degenerate oligonucleotides

IUPAC degenerate bases are supported. Oligonucleotide melting temperature is summarized across concrete sequence variants.

Primer3 evaluates the full concrete variant set used by the representative analysis path. The default **Additional degenerate oligonucleotides analysis** allocates up to 500 additional sequence contexts to ViennaRNA, RNAstructure, seqfold where applicable, and LocalEnumerator. Hairpins and self-dimers are prioritized because their work grows linearly; heterodimer work grows quadratically.

The application calculates the required work before analysis and reports the evaluated/total coverage. If the configured size does not cover the full ensemble, the result is explicitly marked as partial and is not presented as an ensemble-wide worst case.

Each oligonucleotide may expand to at most 256 concrete variants. Larger inputs must be simplified or divided into smaller analyses.

## Exports

Available formats include:

- a human-readable text report;
- a complete CSV or TSV report;
- a CSV containing flagged structures;
- a CSV representation of the dimer matrix.

Machine-readable exports preserve engine-specific measurements, unavailable-state explanations, exact geometry, severity, and analysis coverage. Numeric fields remain numeric or blank rather than mixing values with status text.

## System requirements and limits

- 64-bit CPython 3.12 for source CLI installation on an explicitly supported target;
- Windows 10 22H2 or Windows 11 x86-64 for the verified GUI distribution;
- Ubuntu 24.04 x86-64 for the native desktop/CLI onedir package once its native verification completes;
- an ASCII-only application path for the Windows GUI distribution;
- oligonucleotide sequences from 5 to 60 bases after whitespace removal;
- standard DNA bases and supported IUPAC degenerate codes;
- sufficient time and memory for the selected panel and additional-analysis size.

The models describe unmodified DNA. Fluorophores, quenchers, locked nucleic acids, and other chemical modifications are not included. Predicted values are model-based and can differ from empirical measurements.

## Repository contents

- `release/MBUprime StructLab.exe` and `release/_internal/` form the preserved Windows application snapshot. Newly rebuilt application folders and ZIPs are written to `dist/`; check their bundled provenance for the scientific policy they contain.
- The Ubuntu tarball design places `MBUprime StructLab` and `mbuprime-structlab` beside one shared `_internal/`; it is not onefile and performs no temporary extraction.
- The Ubuntu source route uses `make install`, exact hashed dependency locks, and a locally built RNAstructure extension without changing system Python.
- The top-level Python modules contain the application and scientific orchestration source.
- `rnastructure_native/` contains the in-process RNAstructure extension and validated data tables used by source execution.
- `requirements-targets/` records exact target runtime artifacts; `native/` builds RNAstructure from the pinned source archive and generates the native-host manifest.

## Build from source

The repository includes the native compiler bridge, hashed dependency locks,
asset generators, PyInstaller specifications, and release verification tools.
On Windows x64, use CPython 3.12 and Microsoft Visual C++ Build Tools:

```powershell
python -m pip install --require-hashes -r requirements-build.txt
.\build_exe.bat unsigned
```

The pipeline builds and verifies a new onedir application and ZIP under `dist/`;
it does not replace `release/` or publish anything. See
[the release build contract](packaging/RELEASE.md) for prerequisites, artifact
layout, signing configuration, and verification. Ubuntu uses `make install`
above, or `bash packaging/build_linux_cli.sh` for its onedir bundle.
The Ubuntu bundle verifier requires the `xvfb` and `xauth` system packages,
including on desktop hosts. Install these packages also when using the
`xvfb-run -a make verify-gui` source-install check. macOS uses
`bash packaging/verify_macos_cli.sh` for the CLI-only source route. Run each
platform's build on its native host in an isolated Python environment.

Tests, Windows CI, and local analysis records remain outside the public source
selection. The existing Ubuntu/macOS workflow definitions are retained for
pending native-host validation.

## License

MBUprime StructLab is distributed under the GNU General Public License version 2 (GPL v2); see [COPYING](COPYING). Third-party components and their licenses are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Each binary distribution carries RNAstructure corresponding source under its `_internal/rnastructure-corresponding-source/`; the Ubuntu verifier requires the pinned source archive SHA-256 `4e30fa06f10a89556ad070c8d141fff6090165c330df786e19f9627df4407fd4`.
