# Scientific methods and limitations

[User guide](user-guide.md) · [Validation scope](validation.md)

## Scientific engines

Four thermodynamic engines are mandatory and run inside the application process:

- **Primer3** uses its two-state thermodynamic model.
- **ViennaRNA 2.7.2** uses a loop-based DNA model. For dimers, the reported binding energy separates the intermolecular contribution from the individual strand energies.
- **RNAstructure 6.6** searches for hairpins and dimers and evaluates retained fixed structures.
- **seqfold 0.10.2** evaluates hairpins and fixed hairpin thermodynamics. It does not support dimers.

**LocalEnumerator** is a candidate generator, not a thermodynamic engine. It proposes additional pairing geometries, which the applicable thermodynamic engines then evaluate.

Results from matching geometries are merged. The table-level `dG` is the lowest supported finite value across applicable engines, while hairpin `Tm` uses the highest validated finite value. Engine-specific measurements and provenance remain available in detailed reports and exports.

Gap limits remove unsupported dimer geometries before near-duplicate structures are collapsed. A row labelled **Gap-policy derived** represents a geometry produced by applying those limits to an engine candidate and then recalculating the retained exact structure.

## Degenerate oligonucleotides

IUPAC degenerate bases are supported. Oligonucleotide melting temperature is summarized across concrete sequence variants.

Primer3 evaluates the full concrete variant set used by the representative analysis path. The default **Additional degenerate oligonucleotides analysis** allocates up to 500 additional sequence contexts to ViennaRNA, RNAstructure, seqfold where applicable, and LocalEnumerator. Hairpins and self-dimers are prioritized because their work grows linearly; heterodimer work grows quadratically.

The application calculates the required work before analysis and reports the evaluated/total coverage. If the configured size does not cover the full ensemble, the result is explicitly marked as partial and is not presented as an ensemble-wide worst case.

Each oligonucleotide may expand to at most 256 concrete variants. Larger inputs must be simplified or divided into smaller analyses.

## System requirements and limits

- 64-bit CPython 3.12 for source CLI installation on an explicitly supported target;
- Windows x86-64 for the GUI distribution; executed host evidence is Windows 10 22H2 build 19045, with clean Windows 11 qualification pending;
- Ubuntu 24.04 x86-64 for the native desktop/CLI onedir package once its native verification completes;
- an ASCII-only application path for the Windows GUI distribution;
- oligonucleotide sequences from 5 to 60 bases after whitespace removal;
- standard DNA bases and supported IUPAC degenerate codes;
- sufficient time and memory for the selected panel and additional-analysis size.

The models describe unmodified DNA. Fluorophores, quenchers, locked nucleic acids, and other chemical modifications are not included. Predicted values are model-based and can differ from empirical measurements.

## Scientific provenance

The current scientific manifest schema is 7 and the policy identifier is
`2026-09-08-context-retention-and-duplex-offsets-1`. Reports retain engine identities,
conditions, geometry, coverage, and source/dependency hashes. The authoritative
implementation is [scientific_metadata.py](../scientific_metadata.py), with
orchestration in [thermo_engine.py](../thermo_engine.py).

For upstream scientific references, consult the pinned engines' documentation:
[Primer3](https://primer3.org/), [ViennaRNA](https://www.tbi.univie.ac.at/RNA/),
[RNAstructure](https://rna.urmc.rochester.edu/RNAstructure.html), and
[seqfold](https://github.com/Lattice-Automation/seqfold). These references explain
the upstream models; they do not establish validation of this application's
integration. Component source and licensing provenance are in
[third-party notices](../THIRD_PARTY_NOTICES.md).
