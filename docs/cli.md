# Command-line reference

[Source setup](development.md) · [Worked example](../examples/small-panel/README.md)

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
