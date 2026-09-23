# Repository layout and retention decisions

[Architecture and data flow](architecture.md) · [Development](development.md) · [Complete file inventory](repository-layout.tsv)

## Decision: retain the flat implementation, fix distribution resources

The application is Python with a Tk desktop, a headless CLI and a compiled
RNAstructure bridge. Root implementation modules are shared by the GUI and CLI.
Their import names also occur in PyInstaller collection and scientific source
inventories. Moving them into `src/` would introduce import and compatibility work
without removing a responsibility. They remain the sole implementations; no
compatibility wrappers or second implementation tree are added.

The concrete packaging improvement is to collect GUI resources from the canonical
`assets/` tree into `mbuprime_structlab/assets/` during wheel building. These installed
copies are build outputs. This makes the installed application independent of a
source checkout while preserving the existing source and frozen paths. The extracted
source-distribution/native-wheel gate must check installed CLI analysis and resources
from outside the checkout. See [development verification](development.md).

Documentation navigation is consolidated in the [task index](README.md). Existing
reference paths remain stable. English and Russian quick starts give parallel Windows
onboarding, rather than duplicating the scientific or build reference manuals.

## Root implementation modules

Every module below is retained at its existing path for the shared-import and
provenance contracts described above. Its responsibility is:

| Module | Responsibility |
|---|---|
| `analyzed_run_archive.py` | Typed analyzed-run archive serialization, validation and legacy import |
| `app_assets.py` | Runtime GUI resource resolution for source, installed and frozen layouts |
| `external_engines.py` | Primer3, RNAstructure and seqfold integration and mandatory engine checks |
| `frozen_gui_qualification.py` | Frozen Windows GUI qualification harness |
| `gui_exports.py` | GUI report and matrix exports |
| `gui_results.py` | Result view models and presentation helpers |
| `gui_styles.py` | Tk styling and font configuration |
| `gui_tokens_generated.py` | Generated UI constants from tokens; retain generated consumer beside root GUI modules |
| `headless_geometry.py` | Display-independent structure geometry shared by CLI and GUI |
| `locale_numbers.py` | Locale-aware numeric input parsing and formatting |
| `native_target.py` | Target-specific native artifact and platform identity |
| `nn_thermo.py` | Nearest-neighbor thermodynamic calculation helpers |
| `primer_tool_gui.py` | Desktop entry, input, worker lifecycle and two-column interface |
| `scientific_metadata.py` | Scientific policy, source inventory and engine provenance contracts |
| `structure_draw.py` | Structure diagram rendering |
| `thermo_engine.py` | Scientific orchestration, conditions, reports, candidate handling and severity |
| `vienna_backend.py` | ViennaRNA backend and exact-geometry evaluation |
| `windows_bootstrap.py` | Early Windows DLL, Tk and startup safeguards |

## Boundaries and apparent duplicates

- `mbuprime_structlab/` owns the headless public entry points; it calls the shared root scientific modules. `python primer_tool_gui.py`, `run_tool.bat`, `python -m mbuprime_structlab` and the installed console script keep their existing roles.
- `native/` contains compiler/build machinery and the C++ bridge. `rnastructure_native/` is the runtime package. `vendor/rnastructure-6.6/RNAstructureSource.zip` supplies pinned upstream source; it is not a redundant binary.
- `rnastructure_native/data_tables/scaled/` and `enthalpy_as_dg/` serve different thermodynamic evaluations. Matching filenames do not make their contents interchangeable. Both trees remain intact.
- `setup.py` performs native compilation and distribution hooks; `pyproject.toml` declares build/project metadata; `MANIFEST.in` controls source-archive inputs. Each is required by a different part of the existing build contract.
- `requirements.in` specifies ranges, `requirements.txt` delegates to `requirements-lock.txt`, and that lock pins the Windows scientific/GUI runtime. `requirements-build.txt` adds release tools and `requirements-test.txt` adds tests. `requirements-targets/` separates target artifacts and build/runtime phases; the Windows CLI lock intentionally excludes GUI dependencies.
- `packaging/MBUprimeStructLab.spec` and `MBUprimeStructLabCLI.spec` target different entry points. Platform bootstraps and scripts retain distinct Windows, Ubuntu and macOS responsibilities; their existence is not platform qualification.
- `tokens/*.json` are token/registry inputs; `gui_tokens_generated.py` is their generated consumer. The SVG, ICO and icon generator serve vector source, executable icon and regeneration roles. Font license and source records remain beside the font.
- Root `COPYING`, component licenses under `vendor/` and `assets/`, `THIRD_PARTY_NOTICES.md`, and `docs/license-history/` cover different licensing obligations and historical evidence. None is removed as a duplicate.
- `examples/small-panel/panel.tsv` is strict CLI input; `bulk.txt` is pasteable GUI input; `result.tsv` and the example README preserve executed historical output. The README screenshot has its own version/origin caption; changing the release version does not update historical evidence.

## Current-to-target inventory

[repository-layout.tsv](repository-layout.tsv) has one row per tracked path, plus
explicit new documentation paths. Columns record baseline path, target path,
responsibility, role, decision and rationale. No baseline file is moved or deleted
in this rework. Native identity is regenerated in place by its maintained tool.
Directory families in the inventory retain their platform or scientific contracts;
individual root modules are listed above. New generated wheel resources are not
tracked source files.

When adding or moving a tracked file, update the inventory and any affected imports,
resource lookup, manifests, build specs, tests and links together. Do not include
local acceptance logs, research material, transcripts, private data or build products.
