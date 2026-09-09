# Validation evidence and pending checks

This page describes the scope of evidence; it is not a claim of biological
validation, clinical suitability, exhaustive coverage or benchmark performance.

| Evidence or check | What it establishes | Limit |
|---|---|---|
| [Executed small example](../examples/small-panel/README.md) | A synthetic two-oligo run completed with mandatory engines under the recorded 2.4.3 Windows runtime. | One example; no empirical assay validation or large-panel performance claim. |
| [Public tests](../tests/) | Repeatable regression checks for the included cases; run `python -m pytest tests -q` after setup. | Read actual run summaries and skips; suite presence alone is not a passing run. |
| [Release verification](../packaging/RELEASE.md) | Source/native/dependency identities, packaged content and defined self-test checks. | Hashes are integrity evidence, not publisher authentication or byte-reproducible builds. |
| Windows host evidence | Execution on Windows 10 22H2 build 19045. | Clean Windows 11 qualification remains pending. |
| GUI qualification | Automated self-test and optional archive scenarios have distinct commands. | Isolated large-GUI, manual DPI and accessibility checks remain pending. |
| Ubuntu and macOS workflows | Native-host qualification routes exist. | Unverified until target-host runs and artifact checks complete. |

Report engine versions, scientific policy, conditions and evaluated/total variant
coverage when comparing scientific results. Partial coverage does not establish an
ensemble-wide worst case. See [methods](methods.md) and the saved manifest in each
completed report. Published release notes and checksums are available in
[release history](https://github.com/ilyakichigin92/MBUprime-StructLab/releases).
