"""Pure localized projections for GUI result tables and status text."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from typing import Any

import thermo_engine as te


Translator = Callable[..., str]
TextProjector = Callable[..., str]

_KIND_KEYS = {
    "Hairpin": "option.hairpin",
    "Self-dimer": "option.self_dimer",
    "Hetero-dimer": "option.hetero_dimer",
}
_OLIGO_NAME_KEYS = {
    "Forward primer": "oligo.forward",
    "Reverse primer": "oligo.reverse",
    "Probe 1": "oligo.probe1",
    "Probe 2": "oligo.probe2",
}


def _translate(translator: Translator | None, language: str, key: str,
               **values: object) -> str:
    if translator is None:
        return key.format(**values) if values else key
    return translator(language, key, **values)


def _format_optional(value: object) -> str:
    return "-" if value is None else f"{float(value):.2f}"


def ensemble_coverage_text(
    report: te.AnalysisReport, translator: Translator,
    language: str = "en",
) -> str:
    """Render the GUI's concise ensemble-completeness summary."""

    coverage = report.ensemble_coverage
    if coverage is None:
        return ""
    payload = coverage.to_dict()
    evaluated = payload["evaluated_contexts"]
    total = payload["total_contexts"]
    key = ("status.ensemble_complete" if payload["ensemble_complete"]
           else "status.ensemble_partial")
    return _translate(
        translator, language, key, evaluated=evaluated, total=total)


def _structure_tm_methods(
    *, hairpin: bool, translator: Translator, language: str,
) -> dict[str, str]:
    return {
        "Primer3": _translate(translator, language, "tm.method.primer3"),
        "ViennaRNA": _translate(translator, language, "tm.method.vienna"),
        "RNAstructure": _translate(
            translator, language,
            "tm.method.rnastructure_hairpin" if hairpin
            else "tm.method.rnastructure_dimer"),
        "seqfold": _translate(translator, language, "tm.method.seqfold"),
    }


def _structure_tm_values(
    structure: object,
    external: dict[str, tuple[float | None, float | None]],
) -> dict[str, float | None]:
    return {
        "Primer3": getattr(structure, "tm_p3_c", None),
        "ViennaRNA": getattr(structure, "tm_vienna_c", None),
        "RNAstructure": external.get("RNAstructure", (None, None))[1],
        "seqfold": external.get("seqfold", (None, None))[1],
    }


def _unavailable_tm_entry(
    engine: str, states: dict, translator: Translator, language: str,
) -> tuple[str, None] | None:
    engine_labels = {"RNAstructure": "RNAstruct", "seqfold": "seqfold"}
    if engine not in engine_labels:
        return None
    state = states.get(engine, {}).get("tm", "-")
    if state in {"-", "different_geometry", "no_structure"}:
        return None
    state_text = _translate(translator, language, f"metric_state.{state}")
    return f"{engine_labels[engine]} {state_text}", None


def structure_tm_entries(
        structure: object, translator: Translator,
        language: str = "en") -> list[tuple[str, float | None]]:
    """Return method-labelled Tm values or attributable engine states."""

    if not getattr(structure, "found", True):
        return []
    external = te.ee.engine_metrics(
        getattr(structure, "engine_observations", ()))
    states = getattr(structure, "external_metric_states", {})
    hairpin = getattr(structure, "kind", "") == "Hairpin"
    engines = ["Primer3", "ViennaRNA", "RNAstructure"]
    if hairpin:
        engines.append("seqfold")
    method_labels = _structure_tm_methods(
        hairpin=hairpin, translator=translator, language=language)
    tm_values = _structure_tm_values(structure, external)
    entries: list[tuple[str, float | None]] = []
    for engine in engines:
        value = tm_values[engine]
        if isinstance(value, (int, float)) and math.isfinite(value):
            entries.append((method_labels[engine], float(value)))
            continue
        unavailable = _unavailable_tm_entry(
            engine, states, translator, language)
        if unavailable is not None:
            entries.append(unavailable)
    return entries


def format_structure_tm(structure: object, translator: Translator,
                        language: str = "en") -> str:
    """Combine all supported finite Tm values into one labelled cell."""

    fragments = [
        label if value is None else f"{label} {value:.2f}"
        for label, value in structure_tm_entries(
            structure, translator, language)
    ]
    return " / ".join(fragments) if fragments else "-"


def format_external_dg(structure: object, engine: str,
                       translator: Translator, language: str = "en") -> str:
    """Format external dG state for display without changing raw exports."""

    metrics = te.ee.engine_metrics(
        getattr(structure, "engine_observations", ()))
    value = metrics.get(engine, (None, None))[0]
    if isinstance(value, (int, float)) and math.isfinite(value):
        return f"{value:.2f}"
    state = getattr(structure, "external_metric_states", {}).get(
        engine, {}).get("dg", "-")
    if state in {"different_geometry", "no_structure"}:
        return "-"
    return _translate(translator, language, f"metric_state.{state}")


def problem_result_columns() -> tuple[str, ...]:
    """Return the stable problems-first table column order."""

    return (
        "severity", "kind", "site", "label", "n", "dg",
        "tm", "rank", "discovered", "assessment",
    )


def sort_heading_text(label: str, active: bool,
                      descending: bool) -> str:
    """Return a column label with a non-color active-sort indicator."""

    if not active:
        return label
    return f"{label} {'▼' if descending else '▲'}"


def sort_status_text(column: str, descending: bool,
                     language: str = "en") -> str:
    """Return the localized current-sort announcement."""

    if language == "ru":
        direction = "по убыванию" if descending else "по возрастанию"
        return f"Сортировка: {column} · {direction}"
    direction = "descending" if descending else "ascending"
    return f"Sort: {column} · {direction}"


def structure_pair_count(structure: object) -> int | None:
    """Return canonical geometry pair count when geometry is available."""

    geometry = getattr(structure, "geometry", None)
    pairs = getattr(geometry, "pairs", None)
    return len(pairs) if pairs is not None else None


def _validated_dimer_tm_values(structure: object) -> list[float]:
    """Return finite display Tm values with RNAstructure crossing proof."""

    metrics = getattr(structure, "metrics_by_engine", {})
    values: list[float] = []
    for engine in ("Primer3", "ViennaRNA", "RNAstructure"):
        value = metrics.get(engine, {}).get("tm_c")
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            continue
        if engine == "RNAstructure":
            matching = (
                observation
                for observation in getattr(structure, "engine_observations", ())
                if observation.engine == engine
                and observation.status == "ok"
                and observation.tm_c == value
            )
            if not any(observation.tm_status == "crossing_found"
                       for observation in matching):
                continue
        values.append(float(value))
    return values


def problem_metric_values(
        structure: object) -> tuple[float | None, float | None]:
    """Return aggregate dG/Tm risk values used only by the Flagged table."""

    hairpin = getattr(structure, "kind", "") == "Hairpin"
    if not hasattr(structure, "metrics_by_engine"):
        return None, None
    dgs, hairpin_tms = te._finite_peer_metrics(structure, hairpin=hairpin)
    minimum_dg = min((value for value, _engine in dgs), default=None)
    if hairpin:
        maximum_tm = max(
            (value for value, _engine in hairpin_tms), default=None)
        return minimum_dg, maximum_tm

    dimer_tms = _validated_dimer_tm_values(structure)
    return minimum_dg, max(dimer_tms, default=None)


def _default_kind(kind: str, translator: Translator | None,
                  language: str) -> str:
    return _translate(translator, language, _KIND_KEYS.get(kind, kind))


def _default_oligo_name(name: str, translator: Translator | None,
                        language: str) -> str:
    text = str(name)
    match = re.fullmatch(r"Oligo (\d+)", text)
    if match:
        return _translate(
            translator, language, "bulk.default_name", index=match.group(1))
    key = _OLIGO_NAME_KEYS.get(text)
    return _translate(translator, language, key) if key else text


def _default_label(label: str, kind: str, translator: Translator | None,
                   language: str) -> str:
    text = str(label)
    canonical_kind = kind if kind in _KIND_KEYS else ""
    if not canonical_kind:
        canonical_kind = next(
            (candidate for candidate in _KIND_KEYS
             if text.startswith(candidate + ":")), "")
    if canonical_kind and text.startswith(canonical_kind + ":"):
        text = (_default_kind(canonical_kind, translator, language)
                + text[len(canonical_kind):])
    for canonical_name in _OLIGO_NAME_KEYS:
        text = text.replace(
            canonical_name,
            _default_oligo_name(canonical_name, translator, language))
    return re.sub(
        r"(?<![A-Za-z])Oligo (\d+)",
        lambda match: _translate(
            translator, language, "bulk.default_name", index=match.group(1)),
        text,
    )


def _finding_row_projectors(
    translator: Translator | None,
    language: str,
    *,
    display_severity: TextProjector | None,
    display_kind: TextProjector | None,
    display_site: TextProjector | None,
    display_label: TextProjector | None,
    display_role: TextProjector | None,
    display_assessment: TextProjector | None,
) -> dict[str, TextProjector]:
    """Resolve optional GUI projections to their localized defaults."""

    return {
        "severity": display_severity or (
            lambda value: _translate(
                translator, language, f"severity.{value}")),
        "kind": display_kind or (
            lambda value: _default_kind(value, translator, language)),
        "site": display_site or (
            lambda value: _translate(
                translator, language,
                "site.3prime" if value else "site.internal")),
        "label": display_label or (
            lambda value, kind: _default_label(
                value, kind, translator, language)),
        "role": display_role or (
            lambda value: _translate(
                translator, language, "structure.role.gap_policy_derived")
            if value == "Gap-policy derived" else value),
        "assessment": display_assessment or (lambda value, _kind: value),
    }


def finding_row_values(
        finding: te.ReportFinding, translator: Translator | None = None,
        language: str = "en", *,
        display_severity: TextProjector | None = None,
        display_kind: TextProjector | None = None,
        display_site: TextProjector | None = None,
        display_label: TextProjector | None = None,
        display_role: TextProjector | None = None,
    display_assessment: TextProjector | None = None) -> tuple[Any, ...]:
    """Build one localized row while leaving canonical finding fields intact."""

    project = _finding_row_projectors(
        translator, language, display_severity=display_severity,
        display_kind=display_kind, display_site=display_site,
        display_label=display_label, display_role=display_role,
        display_assessment=display_assessment)
    minimum_dg, maximum_tm = problem_metric_values(finding.structure)
    pair_count = structure_pair_count(finding.structure)
    return (
        project["severity"](finding.severity),
        project["kind"](finding.kind),
        project["site"](finding.involves_3prime),
        project["label"](finding.label, finding.kind),
        "-" if pair_count is None else str(pair_count),
        _format_optional(minimum_dg),
        _format_optional(maximum_tm),
        finding.rank,
        project["role"](te.structure_origin(finding.structure)),
        project["assessment"](finding.assessment, finding.kind),
    )


def _problem_dg_sort_key(finding: te.ReportFinding) -> float:
    minimum_dg, _maximum_tm = problem_metric_values(finding.structure)
    if minimum_dg is None:
        minimum_dg = min(
            (value for value in (finding.dg_p3, finding.dg_vienna)
             if value is not None),
            default=None,
        )
    return minimum_dg if minimum_dg is not None else float("inf")


def _problem_tm_sort_key(finding: te.ReportFinding) -> float:
    _minimum_dg, maximum_tm = problem_metric_values(finding.structure)
    if maximum_tm is None:
        maximum_tm = finding.tm_c
    return maximum_tm if maximum_tm is not None else float("-inf")


def _problem_pair_count_sort_key(finding: te.ReportFinding) -> int:
    count = structure_pair_count(finding.structure)
    return count if count is not None else -1


_PROBLEM_SORT_KEY_PROJECTORS: dict[
    str, Callable[[te.ReportFinding], object]
] = {
    "severity": lambda finding: {
        "problem": 3, "caution": 2, "ok": 1}.get(finding.severity, 0),
    "kind": lambda finding: finding.kind.lower(),
    "site": lambda finding: 1 if finding.involves_3prime else 0,
    "label": lambda finding: finding.label.lower(),
    "n": _problem_pair_count_sort_key,
    "dg": _problem_dg_sort_key,
    "tm": _problem_tm_sort_key,
    "rank": lambda finding: finding.rank,
    "discovered": lambda finding: te.structure_origin(finding.structure),
    "assessment": lambda finding: finding.assessment.lower(),
}


def problem_sort_key(finding: te.ReportFinding, column: str) -> object:
    """Return the stable canonical sort key for a projected result column."""

    projector = _PROBLEM_SORT_KEY_PROJECTORS.get(column)
    return projector(finding) if projector is not None else ""


def sort_problem_findings(
        findings: Sequence[te.ReportFinding], column: str,
        descending: bool) -> list[te.ReportFinding]:
    """Return findings sorted by a canonical field and deterministic ties."""

    return sorted(
        findings,
        key=lambda finding: (
            problem_sort_key(finding, column), finding.kind.lower(),
            finding.label.lower(), finding.rank),
        reverse=descending,
    )
