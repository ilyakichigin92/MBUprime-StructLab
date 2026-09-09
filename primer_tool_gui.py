"""Tk desktop presentation and interaction boundary for MBUprime StructLab.

The GUI collects assay/multiplex inputs and reaction settings, schedules the
scientific engine, localizes its immutable results, and opens shared structure
renderers/exporters. Scientific values, ranking, and policy stay in
``thermo_engine``; this module owns user interaction and presentation.

The frozen self-test is the packaged Tk/font/export/mandatory-engine smoke
boundary used by release verification.
"""

from __future__ import annotations

from io import BytesIO
import math
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk, messagebox, simpledialog

import thermo_engine as te
import vienna_backend as vb
import structure_draw as sd   # safe to import (matplotlib loaded lazily inside)
import gui_exports
import analyzed_run_archive
import gui_results
import gui_styles
from locale_numbers import parse_locale_number
from app_assets import (CASCADIA_MONO_NAME, apply_window_icon,
                        validate_cascadia_mono)

PRODUCT_NAME = "MBUprime StructLab"
_CTRL_MASK = 0x0004
_SHIFT_MASK = 0x0001
_LAYOUT_INDEPENDENT_SHORTCUTS = {
    65: "select_all",  # A
    67: "copy",        # C
    86: "paste",       # V
    88: "cut",         # X
    89: "redo",        # Y
    90: "undo",        # Z
}
_EDIT_SHORTCUTS = {"cut", "paste", "undo", "redo"}

# Compatibility aliases retained for integrations that import legacy tokens.
_COLOR = gui_styles.COLOR
_SPACE = gui_styles.SPACE
_FONT = gui_styles.FONT
_DIMEN = gui_styles.DIMEN
_SEV_COLOR = gui_styles.SEVERITY_COLOR
_SEV_FG = gui_styles.SEVERITY_FOREGROUND
_UI = gui_styles.UI
_STYLE_CONTRACT = gui_styles.STYLE_CONTRACT
_SUMMARY_STATES = gui_styles.SUMMARY_STATES
DEFAULT_ADDITIONAL_ANALYSIS_EXPANDED = False
_FILTER_OPTIONS = {
    "severity": ("Flagged", "All", "Problem", "Caution", "OK"),
    "kind": ("All", "Hairpin", "Self-dimer", "Hetero-dimer"),
}
_FLAGGED_COLUMN_BOUNDS = {
    "severity": (66, 130), "kind": (82, 160), "site": (58, 120),
    "label": (150, 300), "n": (42, 72), "dg": (66, 92),
    "tm": (66, 92), "rank": (54, 90), "discovered": (120, 260),
    "assessment": (330, 480),
}
_STRUCTURE_COLUMN_BOUNDS = {
    "#0": (250, 380), "severity": (66, 130), "site": (58, 120),
    "n": (42, 72), "dg": (66, 92), "tm": (66, 92),
    "rank": (54, 90), "discovered": (120, 260),
    "assessment": (330, 480),
}
_FLAGGED_COLUMNS = (
    "severity", "kind", "site", "label", "n", "dg", "tm", "rank",
    "discovered", "assessment",
)
_STRUCTURE_COLUMNS = (
    "severity", "site", "n", "dg", "tm", "rank", "discovered",
    "assessment",
)
_FLAGGED_DISPLAY_COLUMNS = (
    "severity", "kind", "site", "label", "n", "dg", "tm", "assessment",
)
_STRUCTURE_DISPLAY_COLUMNS = (
    "severity", "site", "n", "dg", "tm", "assessment",
)


def _analysis_failure_category(error_type: str) -> str:
    normalized = error_type.casefold()
    if "incomplete" in normalized:
        return "incomplete"
    if any(marker in normalized for marker in (
            "engine", "vienna", "rnastructure", "seqfold", "import")):
        return "engine"
    return "unexpected"


def _autosize_tree_columns(tree, bounds: dict[str, tuple[int, int]],
                           *, measure_text=None, padding: int = 24) -> None:
    for _ in _autosize_tree_steps(
            tree, bounds, measure_text=measure_text, padding=padding):
        pass


def _autosize_tree_steps(tree, bounds, *, measure_text=None, padding=24):
    """Size headings and all nested rows with bounded, font-aware widths."""

    if measure_text is None:
        try:
            style_name = tree.cget("style") or "Treeview"
            font_name = ttk.Style(tree).lookup(style_name, "font")
            font = tkfont.nametofont(font_name, root=tree)
            measure_text = font.measure
        except (AttributeError, KeyError, RuntimeError, tk.TclError):
            measure_text = lambda text: max(
                (len(line) for line in str(text).splitlines()), default=0) * 7

    def rows(parent=""):
        try:
            children = tree.get_children(parent)
        except (AttributeError, KeyError, tk.TclError):
            return
        for iid in children:
            yield iid
            yield from rows(iid)

    row_ids = []
    for iid in rows():
        row_ids.append(iid)
        yield
    widths = {}
    for column, (minimum, maximum) in bounds.items():
        def candidates():
            try:
                yield tree.heading(column, "text")
            except (AttributeError, KeyError, tk.TclError):
                pass
            for iid in row_ids:
                try:
                    yield (tree.item(iid, "text") if column == "#0"
                           else tree.set(iid, column))
                except (AttributeError, IndexError, KeyError, tk.TclError):
                    continue
        measured = None
        # A saturated column cannot grow; repeated labels share a font measure
        # only within this call, so a later font/language change is measured anew.
        for value in candidates():
            text = str(value)
            if text not in widths:
                widths[text] = measure_text(text)
            measured = (widths[text] if measured is None
                        else max(measured, widths[text]))
            yield
            if measured + padding >= maximum:
                break
        measured = (0 if measured is None else measured) + padding
        tree.column(column, width=max(minimum, min(maximum, measured)))
HELP_TOPICS = {
    "dg_thresholds": {
        "en": (
            "The dG “caution” and “problem” thresholds determine the lowest "
            "finite dG of the structure. A more negative value corresponds to "
            "a more stable structure. These parameters change classification, "
            "but not engine calculations."
        ),
        "ru": (
            "Пороговые значения dG «внимание» и «проблема» определяют "
            "наименьшее конечное dG структуры. Более отрицательное "
            "значение соответствует более стабильной структуре. Эти параметры "
            "меняют классификацию, но не вычисления движков."
        ),
    },
    "dimer_gaps": {
        "en": (
            "Maximum consecutive dimer gaps limits one uninterrupted gap run; "
            "maximum total dimer gaps limits all gaps in the alignment. "
            "Structures outside either limit are omitted before near-duplicate "
            "comparison."
        ),
        "ru": (
            "Максимум последовательных пропусков димера ограничивает одну "
            "непрерывную серию пропусков, а максимум всех пропусков — их общее "
            "число. Структуры вне любого из пределов исключаются до сравнения "
            "близких структур."
        ),
    },
    "near_duplicates": {
        "en": (
            "After the dimer gap limits are applied, retained structures at "
            "the same site are compared by the symmetric difference of their "
            "bonded nucleotide-pair coordinates. A difference strictly less "
            "than n makes them near-duplicates. Dimers retain the lower "
            "supported min dG; hairpins retain the higher validated max Tm; "
            "equal scores use deterministic ordering. Missing comparison "
            "metrics, different kinds or sites, and unequal strand lengths "
            "remain separate. Concrete variants are compared only in the "
            "final union of one logical interaction during complete additional "
            "degenerate-oligonucleotide analysis."
        ),
        "ru": (
            "После применения ограничений пропусков димера сохранённые "
            "структуры одного участка сравниваются по симметрической разности "
            "координат связанных пар нуклеотидов. Разность строго меньше n "
            "означает, что структуры близкие. Для димеров сохраняется меньшее "
            "поддерживаемое мин dG; для шпилек — большее проверенное макс Tm; "
            "при равных значениях используется детерминированный порядок. "
            "Структуры без метрики сравнения, разных типов или участков либо "
            "с цепями разной длины остаются отдельными. Конкретные варианты "
            "сравниваются только в итоговом объединении одного логического "
            "взаимодействия при полном дополнительном анализе вырожденных "
            "олигонуклеотидов."
        ),
    },
    "additional_analysis": {
        "en": (
            "Additional analysis evaluates more concrete combinations of "
            "degenerate oligonucleotides. The size is a bounded allocation. "
            "Coverage is reported explicitly when the complete ensemble does "
            "not fit the allocation."
        ),
        "ru": (
            "Дополнительный анализ проверяет больше конкретных комбинаций "
            "вырожденных олигонуклеотидов. Размер задаёт ограниченный объём "
            "работы. Если весь ансамбль не помещается в этот объём, охват "
            "указывается явно."
        ),
    },
    "engine_methods": {
        "en": (
            "Primer3, ViennaRNA, RNAstructure, and seqfold are mandatory "
            "in-process engines. RNAstructure supplies native hairpin/dimer "
            "search and exact-structure scores; seqfold supplies hairpin-only "
            "fixed-structure thermodynamics. Open Methods / engines for the "
            "complete method statement."
        ),
        "ru": (
            "Primer3, ViennaRNA, RNAstructure и seqfold — обязательные "
            "внутрипроцессные движки. RNAstructure выполняет нативный поиск "
            "шпилек и димеров и оценку точной структуры; seqfold вычисляет "
            "термодинамику фиксированной структуры только для шпилек. Полное "
            "описание приведено в окне «Методы / движки»."
        ),
    },
}
_LANGUAGE_NAMES = {"en": "English", "ru": "Русский"}
_LANGUAGE_BY_NAME = {name: code for code, name in _LANGUAGE_NAMES.items()}
_I18N = {
    "en": {
        "action.analyze": "Analyze",
        "action.close": "Close",
        "action.collapse": "Collapse",
        "action.expand": "Expand",
        "action.help": "Help",
        "ensemble.complete_mode": "Additional degenerate oligonucleotides analysis",
        "ensemble.additional_budget": "Additional analysis size",
        "ensemble.invalid_budget": (
            "Additional analysis size must be an integer greater than or equal to 0."),
        "ensemble.summary_on": "Additional variants: On · {size}",
        "ensemble.summary_off": "Additional variants: Off · {size}",
        "help.dg_thresholds": "dG thresholds",
        "help.dimer_gaps": "Dimer gap limits",
        "help.near_duplicates": "Near-duplicate structures",
        "help.additional_analysis": "Additional analysis",
        "help.engine_methods": "Engine methods",
        "action.cancel": "Cancel",
        "action.clear": "Clear",
        "action.clear_filters": "Clear filters",
        "action.copy": "Copy",
        "action.copy_rows": "Copy selected rows",
        "action.csv_flagged": "CSV flagged structures...",
        "action.csv_matrix": "CSV matrix...",
        "action.csv_report": "CSV full report...",
        "action.cut": "Cut",
        "action.export": "Export...",
        "action.import_run": "Import analyzed run...",
        "action.run_archive": "Analyzed run archive...",
        "action.save_preset": "Save preset",
        "action.delete_preset": "Delete preset",
        "action.paste": "Paste",
        "action.reset_defaults": "Reset defaults",
        "action.rules": "Rules / engines",
        "action.select_all": "Select all",
        "action.show_details": "Show details",
        "action.hide_details": "Hide details",
        "action.show_diagram": "Show diagram",
        "action.text_report": "Text report...",
        "action.tsv_report": "TSV full report...",
        "bulk.count": "{n} oligos  ->  {pairs} hetero-dimer pair(s){suffix}",
        "bulk.ignored_suffix": "; {ignored} ignored line(s)",
        "bulk.help": (
            "Paste a whole panel: ONE oligo per line. Each line may be a "
            "bare sequence or 'Name = SEQUENCE'. Every pair is checked; "
            "labels containing 'pr' or 'probe' use the probe concentration."
        ),
        "bulk.preview": "Parsed oligos preview",
        "bulk.zero": "0 oligos",
        "cond.invalid": "Condition fields need valid numbers before analysis.",
        "cond.label.dg_caution": "dG caution",
        "cond.label.dg_problem": "dG problem",
        "cond.label.dg_temp_c": "dG temp (C)",
        "cond.label.dntp_conc": "dNTPs (mM)",
        "cond.label.dv_conc": "Mg2+ (mM)",
        "cond.label.mv_conc": "Na+/K+ (mM)",
        "cond.label.primer_conc": "Primer (nM)",
        "cond.label.probe_conc": "Probe (nM)",
        "cond.label.near_duplicate_bond_difference": "Near-duplicate bond difference (<n)",
        "cond.label.dimer_max_consecutive_gaps": "Dimer max consecutive gaps",
        "cond.label.dimer_max_total_gaps": "Dimer max total gaps",
        "cond.must_be_number": "{label} must be a number (got '{raw}')",
        "cond.preset": "Preset",
        "preset.name_prompt": "Name for the current reaction-condition preset:",
        "preset.name_title": "Save condition preset",
        "preset.overwrite_title": "Replace preset?",
        "preset.overwrite_body": "A preset named '{name}' already exists. Replace it?",
        "preset.delete_title": "Delete preset?",
        "preset.delete_body": "Delete the custom preset '{name}'?",
        "preset.saved_title": "Preset saved",
        "preset.saved_body": "The condition preset '{name}' was saved for this user.",
        "preset.deleted_title": "Preset deleted",
        "preset.deleted_body": "The custom preset '{name}' was deleted.",
        "preset.error_title": "Preset error",
        "preset.load_error": (
            "Custom presets could not be loaded. The built-in preset remains available.\n\n"
            "Preset file:\n{path}\n\nTechnical details:\n{error}"),
        "preset.custom_required": "Select a saved custom preset to delete it.",
        "preset.unsaved": "Current conditions have unsaved changes.",
        "preset.saved_inline": "Saved preset: {name}",
        "preset.deleted_inline": "Deleted preset: {name}",
        "cond.summary": (
            "{preset} | Mg2+ {mg:g} mM | primer {primer:g} nM | "
            "probe {probe:g} nM | dG flags {dg_caution:g}/{dg_problem:g} "
            "kcal/mol | near <{near_difference} bonds | dimer gaps "
            "run/total {gap_run}/{gap_total}"
        ),
        "detail.empty_body": "{label}\n\n{assessment}",
        "detail.none_selected": "No structure selected",
        "detail.none_title": "Selected structure - none selected",
        "detail.select_prompt": "Select a row or matrix cell to inspect the predicted pairing.",
        "detail.selected": "Selected structure",
        "detail.status": "{role} | {severity} | {site}",
        "detail.engine_metrics": "engine metrics",
        "detail.engine_metrics_unavailable": "engine metrics unavailable",
        "dialog.calc_error": "Calculation error",
        "archive.save_title": "Export analyzed run",
        "archive.open_title": "Import analyzed run",
        "archive.file_type": "Compressed analyzed run (*.mbusl-run)",
        "archive.file_type_legacy": "Legacy analyzed run (*.json)",
        "archive.export_failed": "Analyzed run export failed",
        "archive.export_failed_body": (
            "The analyzed run was not saved. Any existing destination file was not "
            "changed.\n\nSelected path:\n{path}\n\nTechnical details:\n{error}"),
        "archive.exported": "Analyzed run exported",
        "archive.exported_body": "The analyzed run was saved to:\n{path}",
        "archive.import_failed": "Analyzed run import failed",
        "archive.import_failed_body": (
            "The selected run was not loaded. Existing inputs and results were not changed.\n\n"
            "Selected path:\n{path}\n\nTechnical details:\n{error}"),
        "archive.imported": "Analyzed run imported",
        "archive.imported_body": (
            "The saved oligonucleotides, reaction conditions, controls, and completed "
            "results were restored without running scientific engines."),
        "archive.apply_failed_body": (
            "The saved run could not be applied to the window. The previous inputs and "
            "results were restored.\n\nTechnical details:\n{error}"),
        "archive.preview_title": "Review analyzed run",
        "archive.preview_heading": "Import this completed analysis?",
        "archive.preview_body": (
            "Oligonucleotides: {oligos}\nConditions: {conditions}\n"
            "Additional analysis: {mode}; budget {budget}\n"
            "Coverage: {evaluated}/{total} ({completeness})\n"
            "Source app: {version}\nScientific policy: {policy}\n"
            "Analysis ID: {analysis_id}"),
        "archive.preview_historical_policy": (
            "This archive uses a recognized historical scientific policy. Its saved "
            "results will be restored exactly and will not be recalculated under the "
            "current policy."),
        "archive.preview_complete": "complete",
        "archive.preview_partial": "partial",
        "archive.confirm_import": "Import",
        "dialog.failure_summary.engine": (
            "A required scientific engine could not complete the analysis."),
        "dialog.failure_recovery.engine": (
            "Close and reopen the application, then run its self-test. If the "
            "self-test still fails, reinstall the official package before analyzing again."),
        "dialog.failure_summary.incomplete": (
            "The analysis ended before a complete report was available."),
        "dialog.failure_recovery.incomplete": (
            "Check the sequence inputs and reaction conditions, then choose Analyze again."),
        "dialog.failure_summary.unexpected": (
            "The analysis stopped because of an unexpected calculation error."),
        "dialog.failure_recovery.unexpected": (
            "Check the sequence inputs and reaction conditions, then choose Analyze again. "
            "If it repeats, copy the technical diagnostics when requesting support."),
        "dialog.failure_technical": "Technical diagnostics:\n{error_type}: {error}",
        "dialog.clear_body": "This clears all sequence inputs and the current results. Continue?",
        "dialog.clear_title": "Clear inputs and results?",
        "dialog.diagram_body": "This row has no folded structure to draw.",
        "dialog.diagram_error": (
            "Drawing diagrams requires matplotlib.\n"
            "Install it with:  pip install matplotlib\n\n"
            "({error_type}: {error})"
        ),
        "dialog.diagram_title": "Structure diagram",
        "dialog.export_failed": "Export failed",
        "dialog.input_error": "Input error",
        "dialog.report_saved": "Report saved",
        "dialog.report_saved_body": "Report written to:\n{path}",
        "dialog.rules_title": "Rules and engines",
        "dialog.engine_methods": (
            "Primer3, ViennaRNA, RNAstructure, and seqfold are thermodynamic "
            "engines. All four engines are mandatory and run in-process."
        ),
        "dialog.rules_body": (
            "Warning purpose:\n\n"
            "dG thresholds: caution <= {dg_caution:g} kcal/mol; "
            "problem <= {dg_problem:g} kcal/mol.\n"
            "When multiple engines score a structure, the more stable dG is used.\n\n"
            "Hairpins have an additional stem/Tm threshold:\n"
            "caution: 3 pairs >55 C, 4 >50 C, 5 >45 C, 6 >40 C, 7+ always caution\n"
            "problem: 3 pairs >65 C, 4 >60 C, 5 >55 C, 6+ >50 C\n"
            "When multiple engines score a hairpin, the higher Tm is used.\n\n"
            "Oligonucleotide melting temperatures:\n\n"
            "Tm SL and Tm Owcz are oligonucleotide melting temperatures, not "
            "the Tm of a specific structure. Tm SL uses the SantaLucia salt "
            "correction, while Tm Owcz uses the Owczarzy mixed-salt correction. "
            "Both calculations use the configured monovalent-ion, divalent-ion, "
            "dNTP, and oligonucleotide concentrations. For a degenerate "
            "oligonucleotide, the min-max range is shown when the variant spread "
            "is greater than 0.05 C; otherwise, the mean is shown.\n\n"
            "Secondary-structure calculation engines:\n\n"
            "{engine_method_text}\n\n"
            "{vienna_text}\n\n"
            "{bundled_engine_text}"
        ),
        "dialog.vienna_available": (
            "Primer3 - the two-state model from the Primer3 package.\n\n"
            "ViennaRNA - loop-model evaluation from the ViennaRNA 2.7.2 "
            "package; for dimers, Vienna uses binding energy "
            "E(AB)-E(A)-E(B)."
        ),
        "dialog.vienna_missing": (
            "Primer3 - the two-state model from the Primer3 package.\n\n"
            "ViennaRNA - loop-model evaluation from the ViennaRNA 2.7.2 "
            "package; for dimers, Vienna uses binding energy "
            "E(AB)-E(A)-E(B).\n\n"
            "ViennaRNA is a mandatory in-process engine but is unavailable. "
            "Analysis is blocked until ViennaRNA passes startup validation."
        ),
        "dialog.bundled_engines": (
            "RNAstructure 6.6 - searches for hairpins and dimers and performs "
            "exact evaluation of a specified structure. Hairpin Tm is determined "
            "as the dG=0 transition for a fixed CT structure; dimer Tm is "
            "calculated using a P3-style concentration-corrected method.\n\n"
            "seqfold 0.10.2 - supports hairpins only and calculates fixed-structure "
            "thermodynamics: it follows one selected MFE path and uses the fixed "
            "structure's dH/dS to find a validated Tm transition at dG=0. "
            "seqfold does not support dimers and does not model salt or strand "
            "concentration.\n\n"
            "LocalEnumerator generates candidate structures; it is not a "
            "thermodynamic engine and does not calculate dG or Tm. Retained "
            "structures are evaluated by every applicable thermodynamic "
            "engine.\n\n"
            "Gap-policy derived - a provenance label, not a thermodynamic "
            "engine. It means the structure was obtained by applying the "
            "configured dimer-gap settings to an engine candidate. Each retained "
            "exact structure is rescored by every applicable thermodynamic "
            "engine."
        ),
        "assessment.concise.acceptable": "acceptable",
        "assessment.concise.interfere": "likely to interfere with PCR",
        "assessment.concise.unclassified": "not classified",
        "assessment.concise.min_dg": "min dG {value}",
        "assessment.concise.max_tm": "max Tm {value}",
        "assessment.concise.n": "n={value}",
        "export.csv_flagged": "CSV flagged structures",
        "export.csv_matrix": "CSV matrix",
        "export.csv_report": "CSV report",
        "export.all_files": "All files",
        "export.save_title": "Save report",
        "export.text_report": "Text report",
        "export.tsv_report": "TSV report",
        "filter.active_default": "Filters: severity=Flagged",
        "filter.active_prefix": "Filters: ",
        "filter.search_part": "search={search}",
        "filter.severity_part": "severity={value}",
        "filter.kind_part": "type={value}",
        "filter.prime_only_part": "3' risks only",
        "filter.showing": "Showing {shown} of {total} structures",
        "filter.showing_zero": "Showing 0 of 0 structures",
        "filter.search": "Search",
        "filter.severity": "Severity",
        "filter.type": "Type",
        "filter.prime_only": "3' risks only",
        "heading.assessment": "Assessment",
        "heading.conc": "Conc (nM)",
        "heading.dg": "dG",
        "heading.dg_p3": "dG P3",
        "heading.dg_vienna": "dG Vienna",
        "heading.dg_rnastructure": "dG RNAstruct",
        "heading.tm_rnastructure": "Tm RNAstruct (P3-style dimers)",
        "heading.dg_seqfold": "dG seqfold",
        "heading.tm_seqfold": "Tm seqfold",
        "heading.flag": "Site",
        "heading.gc": "GC %",
        "heading.kind": "Type",
        "heading.label": "Label",
        "heading.len": "Len",
        "heading.name": "Name",
        "heading.n": "n",
        "heading.oligo": "Oligo",
        "heading.role": "Role",
        "heading.sequence": "Sequence (5'->3')",
        "heading.severity": "Severity",
        "heading.rank": "Rank",
        "heading.discovered_by": "Discovered by",
        "heading.structure": "Ranked structure",
        "heading.tm": "Tm",
        "heading.tm_c": "Tm (C)",
        "heading.tm_engines": "Tm by engine (C)",
        "heading.tm_owcz": "Tm Owcz",
        "heading.tm_sl": "Tm SL",
        "heading.variants": "Variants",
        "input.bulk_tab": "Bulk / multiplex (one per line)",
        "input.forward": "Forward primer",
        "input.help": "Enter at least one oligo. A single oligo reports hairpins and self-dimers only.",
        "input.probe1": "Probe 1",
        "input.probe2": "Probe 2",
        "input.reverse": "Reverse primer",
        "input.single_tab": "Single assay (F / R / probes)",
        "label.language": "Language",
        "matrix.empty": "No dimer results to display. Analyze oligos to populate the matrix.",
        "matrix.legend": "** Problem   ! Caution   3' Risk   Diagonal Self",
        "matrix.no_cells": "No dimer cells to inspect.",
        "matrix.run_first": "Run analysis to populate matrix.",
        "matrix.status_cell": "{label}: {severity}, dG {dg}; rank #{rank}",
        "matrix.status_click": "Click a cell to inspect",
        "matrix.show_flagged": "Show flagged cells only",
        "matrix.legend.problem": "** Problem",
        "matrix.legend.caution": "! Caution",
        "matrix.legend.prime": "3' Risk",
        "matrix.legend.self": "Diagonal Self",
        "option.all": "All",
        "option.caution": "Caution",
        "option.flagged": "Flagged",
        "option.hairpin": "Hairpin",
        "option.hetero_dimer": "Hetero-dimer",
        "option.ok": "OK",
        "option.problem": "Problem",
        "option.self_dimer": "Self-dimer",
        "option.custom": "Custom",
        "overview.cautions": "Cautions",
        "overview.looks_clean": "Looks clean",
        "overview.no_flagged": "Top risks: none flagged across {count} oligo(s).",
        "overview.not_analyzed": "Not analyzed",
        "overview.prime_risks": "3' risks",
        "overview.problems": "Problems",
        "overview.review_cautions": "Cautions only",
        "overview.review_problems": "Review problems",
        "overview.top_risks": "Top risks: {items}",
        "overview.top_risks_idle": "Top risks: run analysis to triage secondary structures.",
        "overview.risk_item": "{label} | {severity} | {site} | rank #{rank}",
        "overview.more": "+{count} more",
        "overview.verdict": "Verdict",
        "overview.worst": "Ranked structures",
        "progress.cancelled": "Cancelled",
        "progress.cancelling": "Cancelling after current step...",
        "progress.complete": "Analysis complete",
        "progress.failed": "Analysis failed",
        "progress.idle": "Idle",
        "progress.starting": "Starting analysis...",
        "progress.starting_estimate": (
            "Analysis is running: checking and initializing mandatory engines; "
            "loading and preparing thermodynamic models. "
            "Variant contexts: {allocated}/{total}"),
        "progress.preparing": (
            "Analysis is running: checking and initializing mandatory engines; "
            "loading and preparing thermodynamic models"),
        "run.title": "Run",
        "section.conditions": "Conditions",
        "severity.caution": "CAUTION",
        "severity.ok": "OK",
        "severity.unclassified": "Unclassified",
        "severity.problem": "PROBLEM",
        "site.3prime": "3' end",
        "site.internal": "internal",
        "status.analysis_cancelled": "Analysis cancelled.{tail}",
        "status.analysis_failed": (
            "Analysis failed. Correct the reported issue and choose Analyze again.{tail}"),
        "status.analysis_running": "Analysis running... Previous results stay visible until this run finishes.",
        "status.analysis_running_previous": "Analysis running — previous complete result remains visible.",
        "status.analysis_running_empty": "Analysis running — no current result is available yet.",
        "status.analysis_current": "Current analysis result.",
        "sort.default": "Sort: default",
        "status.core_complete": "Primer3/ViennaRNA results are ready; RNAstructure/seqfold calculations are still running (not exportable yet).",
        "status.external_pending": "RNAstructure/seqfold calculations pending",
        "status.cancelled_tail": " Previous results kept.",
        "status.enter_sequences": "Enter sequences and click Analyze.",
        "status.external_idle": "Built-in engines (Primer3, ViennaRNA, RNAstructure, seqfold): checked when analysis runs.",
        "status.external_health": "Built-in engine health: {items}",
        "status.engine_ready": "Engines ready",
        "status.ensemble_complete": (
            "Additional degenerate oligonucleotides analysis complete: "
            "{evaluated}/{total}."),
        "status.ensemble_partial": (
            "Partial additional degenerate oligonucleotides analysis complete: "
            "{evaluated}/{total}. Omitted variants may contain stronger "
            "structures; this is not an ensemble-worst result."),
        "status.ensemble_failed": "{count} failed",
        "status.engine_unavailable": "{engine} unavailable",
        "status.engine_interactions": "{engine} {covered}/{total} interactions",
        "status.no_liabilities": "No significant hairpin or dimer liabilities detected.",
        "status.no_problems": "No problems, {cautions} caution(s) - review flagged structures below.",
        "status.problems": "{problems} problem(s), {cautions} caution(s) - review flagged structures below.",
        "structure.header": (
            "{label}  ({role})\n"
            "dG(P3) = {dg:.2f}{vienna}{tm}   [{severity}, {site}]\n"
        ),
        "structure.header_fixed": (
            "{label}  ({role})\n"
            "dG(Vienna fixed structure) = {dg:.2f}{tm}   [{severity}, {site}]\n"
        ),
        "structure.vienna": "   dG(Vienna) = {dg:.2f}",
        "structure.tm_both": "   Tm Primer3 = {tm_p3:.2f} C   Tm Vienna fixed-structure = {tm_vienna:.2f} C",
        "structure.tm_single": "   Tm Primer3 = {tm:.2f} C",
        "structure.tm_fixed": "   Fixed-structure dG=0 Tm = {tm:.2f} C",
        "structure.representative_variant": "representative of {count} variant combinations (non-Primer3 coverage is incomplete): {variant}\n",
        "structure.role.gap_policy_derived": "Gap-policy derived",
        "tm.method.primer3": "P3 two-state",
        "tm.method.vienna": "Vienna fixed",
        "tm.method.rnastructure_hairpin": "RNAstruct fixed CT",
        "tm.method.rnastructure_dimer": "RNAstruct P3-style",
        "tm.method.seqfold": "seqfold fixed-structure dH/dS",
        "metric_state.calculation_failed": "calculation_failed",
        "metric_state.not_installed": "not_installed",
        "metric_state.not_reported": "not_reported",
        "metric_state.not_run": "not_run",
        "metric_state.no_sign_change": "no_sign_change",
        "metric_state.below_search_range": "below_search_range",
        "metric_state.above_search_range": "above_search_range",
        "metric_state.path_changed": "path_changed",
        "metric_state.non_affine": "non_affine",
        "metric_state.not_calculated": "not_calculated",
        "metric_state.engine_error": "engine_error",
        "metric_state.parse_error": "parse_error",
        "metric_state.unsupported": "unsupported",
        "unit.dg": "{value} kcal/mol",
        "unit.nt": "{n} nt",
        "bulk.default_name": "Oligo {index}",
        "oligo.forward": "Forward primer",
        "oligo.reverse": "Reverse primer",
        "oligo.probe1": "Probe 1",
        "oligo.probe2": "Probe 2",
        "tab.flagged": "Flagged structures",
        "tab.hairpins": "Hairpins",
        "tab.hetero": "Hetero-dimers",
        "tab.matrix": "Matrix",
        "tab.self": "Self-dimers",
        "tab.structures": "Structures",
        "tab.tm": "Melting temperatures",
        "table.no_match": "No structures match these filters.",
        "table.no_match_hint": "Clear filters to show all results.",
        "validation.bulk_empty": "Enter at least one oligo (one per line) in the Bulk / multiplex tab.",
    },
    "ru": {
        "action.close": "Закрыть",
        "action.collapse": "Свернуть",
        "action.expand": "Развернуть",
        "action.help": "Справка",
        "ensemble.summary_on": "Дополнительные варианты: Вкл · {size}",
        "ensemble.summary_off": "Дополнительные варианты: Выкл · {size}",
        "help.dg_thresholds": "Пороговые значения dG",
        "help.dimer_gaps": "Ограничения пропусков димера",
        "help.near_duplicates": "Близкие структуры",
        "help.additional_analysis": "Дополнительный анализ",
        "help.engine_methods": "Методы движков",
        "status.analysis_running_previous": "Выполняется анализ — предыдущий полный результат остаётся видимым.",
        "status.analysis_running_empty": "Выполняется анализ — текущего результата пока нет.",
        "status.analysis_current": "Текущий результат анализа.",
        "sort.default": "Сортировка: по умолчанию",
        "action.analyze": "Анализ",
        "ensemble.complete_mode": "Дополнительный анализ вырожденных нуклеотидов",
        "ensemble.additional_budget": "Размер дополнительного анализа",
        "ensemble.invalid_budget": (
            "Размер дополнительного анализа должен быть целым числом не меньше 0."),
        "action.cancel": "Отмена",
        "action.clear": "Очистить",
        "action.clear_filters": "Сбросить фильтры",
        "action.copy": "Копировать",
        "action.csv_flagged": "CSV маркированных структур...",
        "action.csv_matrix": "CSV матрицы...",
        "action.csv_report": "Полный CSV отчет...",
        "action.cut": "Вырезать",
        "action.export": "Экспорт...",
        "action.import_run": "Импортировать анализ...",
        "action.run_archive": "Архив выполненного анализа...",
        "action.save_preset": "Сохранить параметры",
        "action.delete_preset": "Удалить параметры",
        "action.paste": "Вставить",
        "action.reset_defaults": "Сбросить",
        "action.rules": "Правила / движки",
        "action.select_all": "Выделить все",
        "action.show_details": "Показать детали",
        "action.hide_details": "Скрыть детали",
        "action.show_diagram": "Показать схему",
        "action.text_report": "Текстовый отчет...",
        "action.tsv_report": "Полный TSV отчет...",
        "bulk.count": "{n} олигонуклеотидов  ->  {pairs} пар гетеродимеров{suffix}",
        "bulk.ignored_suffix": "; пропущено строк: {ignored}",
        "bulk.help": (
            "Вставьте всю панель: один олигонуклеотид на строку. Строка может быть "
            "просто последовательностью или 'Имя = ПОСЛЕДОВАТЕЛЬНОСТЬ'. "
            "Проверяются все пары; метки с 'pr' или 'probe' используют "
            "концентрацию зонда."
        ),
        "bulk.preview": "Предпросмотр распознанных олигонуклеотидов",
        "bulk.zero": "0 олигонуклеотидов",
        "cond.invalid": "В условиях должны быть корректные числа перед анализом.",
        "cond.label.dg_caution": "dG внимание",
        "cond.label.dg_problem": "dG проблема",
        "cond.label.dg_temp_c": "Темп. dG (C)",
        "cond.label.dntp_conc": "dNTP (мМ)",
        "cond.label.dv_conc": "Mg2+ (мМ)",
        "cond.label.mv_conc": "Na+/K+ (мМ)",
        "cond.label.primer_conc": "Праймер (нМ)",
        "cond.label.probe_conc": "Зонд (нМ)",
        "cond.label.near_duplicate_bond_difference": "Различие связей близких структур (<n)",
        "cond.label.dimer_max_consecutive_gaps": "Макс. последовательных пропусков димера",
        "cond.label.dimer_max_total_gaps": "Макс. всего пропусков димера",
        "cond.must_be_number": "{label} должно быть числом (получено '{raw}')",
        "cond.preset": "Параметры",
        "preset.name_prompt": "Название набора параметров условий реакции:",
        "preset.name_title": "Сохранить параметры условий",
        "preset.overwrite_title": "Заменить параметры?",
        "preset.overwrite_body": "Набор параметров с названием «{name}» уже существует. Заменить его?",
        "preset.delete_title": "Удалить параметры?",
        "preset.delete_body": "Удалить пользовательские параметры «{name}»?",
        "preset.saved_title": "Параметры сохранены",
        "preset.saved_body": "Параметры условий «{name}» сохранены для этого пользователя.",
        "preset.deleted_title": "Параметры удалены",
        "preset.deleted_body": "Пользовательские параметры «{name}» удалены.",
        "preset.error_title": "Ошибка параметров условий",
        "preset.load_error": (
            "Пользовательские параметры не удалось загрузить. Встроенные параметры остаются доступными.\n\n"
            "Файл параметров:\n{path}\n\nТехнические сведения:\n{error}"),
        "preset.custom_required": "Выберите сохранённые пользовательские параметры для удаления.",
        "cond.summary": (
            "{preset} | Mg2+ {mg:g} мМ | праймер {primer:g} нМ | "
            "зонд {probe:g} нМ | флаги dG {dg_caution:g}/{dg_problem:g} "
            "ккал/моль | близкие <{near_difference} связей | пропуски "
            "димера {gap_run}/{gap_total}"
        ),
        "detail.empty_body": "{label}\n\n{assessment}",
        "detail.none_selected": "Структура не выбрана",
        "detail.none_title": "Выбранная структура - ничего не выбрано",
        "detail.select_prompt": "Выберите строку или ячейку матрицы, чтобы посмотреть предсказанную структуру.",
        "detail.selected": "Выбранная структура",
        "detail.status": "{role} | {severity} | {site}",
        "detail.engine_metrics": "вычисления движка",
        "detail.engine_metrics_unavailable": "вычисления движка недоступны",
        "dialog.calc_error": "Ошибка расчета",
        "archive.save_title": "Экспорт выполненного анализа",
        "archive.open_title": "Импорт выполненного анализа",
        "archive.file_type": "Сжатый архив анализа (*.mbusl-run)",
        "archive.file_type_legacy": "Устаревший архив анализа (*.json)",
        "archive.export_failed": "Не удалось экспортировать анализ",
        "archive.export_failed_body": (
            "Выполненный анализ не сохранён. Существующий файл назначения не "
            "изменён.\n\nВыбранный путь:\n{path}\n\nТехнические сведения:\n{error}"),
        "archive.exported": "Анализ экспортирован",
        "archive.exported_body": "Выполненный анализ сохранён в:\n{path}",
        "archive.import_failed": "Не удалось импортировать анализ",
        "archive.import_failed_body": (
            "Выбранный анализ не загружен. Текущие данные и результаты не изменены.\n\n"
            "Выбранный путь:\n{path}\n\nТехнические сведения:\n{error}"),
        "archive.imported": "Анализ импортирован",
        "archive.imported_body": (
            "Сохранённые олигонуклеотиды, условия реакции, параметры и готовые "
            "результаты восстановлены без запуска научных движков."),
        "archive.apply_failed_body": (
            "Сохранённый анализ не удалось применить к окну. Предыдущие данные и "
            "результаты восстановлены.\n\nТехнические сведения:\n{error}"),
        "dialog.failure_summary.engine": (
            "Обязательный научный движок не смог завершить анализ."),
        "dialog.failure_recovery.engine": (
            "Закройте и снова откройте приложение, затем запустите самопроверку. "
            "Если самопроверка снова не пройдет, переустановите официальный пакет "
            "перед повторным анализом."),
        "dialog.failure_summary.incomplete": (
            "Анализ завершился до получения полного отчета."),
        "dialog.failure_recovery.incomplete": (
            "Проверьте последовательности и условия реакции, затем снова нажмите «Анализ»."),
        "dialog.failure_summary.unexpected": (
            "Анализ остановлен из-за непредвиденной ошибки расчета."),
        "dialog.failure_recovery.unexpected": (
            "Проверьте последовательности и условия реакции, затем снова нажмите «Анализ». "
            "Если ошибка повторится, приложите техническую диагностику к запросу поддержки."),
        "dialog.failure_technical": "Техническая диагностика:\n{error_type}: {error}",
        "dialog.clear_body": "Это очистит все последовательности и текущие результаты. Продолжить?",
        "dialog.clear_title": "Очистить ввод и результаты?",
        "dialog.diagram_body": "Для этой строки нет свернутой структуры для схемы.",
        "dialog.diagram_error": (
            "Для построения схем нужен matplotlib.\n"
            "Установите его:  pip install matplotlib\n\n"
            "({error_type}: {error})"
        ),
        "dialog.diagram_title": "Схема структуры",
        "dialog.export_failed": "Экспорт не выполнен",
        "dialog.input_error": "Ошибка ввода",
        "dialog.report_saved": "Отчет сохранен",
        "dialog.report_saved_body": "Отчет записан в:\n{path}",
        "dialog.rules_title": "Правила и движки",
        "dialog.engine_methods": (
            "Primer3, ViennaRNA, RNAstructure и seqfold - термодинамические "
            "движки. Все четыре движка обязательны и работают внутри процесса."
        ),
        "dialog.rules_body": (
            "Назначение предупреждений:\n\n"
            "Пороги dG: внимание <= {dg_caution:g} ккал/моль; "
            "проблема <= {dg_problem:g} ккал/моль.\n"
            "Если структуру оценивают несколько движков, используется более стабильное dG.\n\n"
            "Для шпилек есть дополнительный порог основание/Tm:\n"
            "внимание: 3 пары >55 C, 4 >50 C, 5 >45 C, 6 >40 C, 7+ всегда внимание\n"
            "проблема: 3 пары >65 C, 4 >60 C, 5 >55 C, 6+ >50 C\n"
            "Если шпильку оценивают несколько движков, используется более высокое Tm\n\n"
            "Температуры плавления олигонуклеотидов:\n\n"
            "Tm SL и Tm Owcz — температуры плавления олигонуклеотида, а не Tm "
            "конкретной структуры. Tm SL использует поправку на соли SantaLucia, "
            "а Tm Owcz — поправку Owczarzy для смешанных солей. Оба расчёта "
            "используют концентрации одновалентных и двухвалентных ионов, dNTP "
            "и олигонуклеотида из настроек. Для вырожденного олигонуклеотида "
            "показывается диапазон мин-макс, если разброс вариантов больше "
            "0.05 C; иначе показывается среднее.\n\n"
            "Движки для вычисления вторичных структур:\n\n"
            "{engine_method_text}\n\n"
            "{vienna_text}\n\n"
            "{bundled_engine_text}"
        ),
        "dialog.vienna_available": (
            "Primer3 - модель двух состояний из пакета Primer3.\n\n"
            "ViennaRNA - оценка по петлевой модели из пакета ViennaRNA 2.7.2, "
            "для димеров Vienna использует энергию связывания "
            "E(AB)-E(A)-E(B)."
        ),
        "dialog.vienna_missing": (
            "Primer3 - модель двух состояний из пакета Primer3.\n\n"
            "ViennaRNA - оценка по петлевой модели из пакета ViennaRNA 2.7.2, "
            "для димеров Vienna использует энергию связывания "
            "E(AB)-E(A)-E(B).\n\n"
            "ViennaRNA — обязательный движок внутри процесса, но сейчас он "
            "недоступен. Анализ заблокирован, пока ViennaRNA не пройдет "
            "проверку при запуске."
        ),
        "dialog.bundled_engines": (
            "RNAstructure 6.6 - выполняет поиск шпилек и димеров и точную оценку "
            "заданной структуры. Tm шпильки определяется как переход dG=0 для "
            "фиксированной CT-структуры; Tm димера рассчитывается методом, "
            "скорректированным на концентрацию в стиле P3\n\n"
            "seqfold 0.10.2 - поддерживает только шпильки и рассчитывает "
            "термодинамику фиксированной структуры: он следует одному выбранному "
            "пути MFE и по dH/dS фиксированной структуры находит проверенный "
            "переход Tm при dG=0. seqfold не поддерживает димеры и не моделирует "
            "соль или концентрацию цепей.\n\n"
            "LocalEnumerator генерирует структуры-кандидаты; это не "
            "термодинамический движок, не рассчитывает dG или Tm. Сохранённые "
            "структуры оцениваются каждым применимым термодинамическим "
            "движком.\n\n"
            "Получено по правилам пропусков (Gap-policy derived) - метка "
            "происхождения, а не термодинамический движок. Она означает, что "
            "структура получена применением настроек по пропускам в димерах к "
            "кандидату движка. Каждая сохранённая точная структура повторно "
            "оценивается всеми применимыми термодинамическими движками."
        ),
        "assessment.concise.acceptable": "приемлемо",
        "assessment.concise.interfere": "может помешать ПЦР",
        "assessment.concise.unclassified": "не классифицировано",
        "assessment.concise.min_dg": "мин dG {value}",
        "assessment.concise.max_tm": "макс Tm {value}",
        "assessment.concise.n": "n={value}",
        "export.csv_flagged": "CSV маркированных структур",
        "export.csv_matrix": "CSV матрицы",
        "export.csv_report": "CSV отчет",
        "export.all_files": "Все файлы",
        "export.save_title": "Сохранить отчет",
        "export.text_report": "Текстовый отчет",
        "export.tsv_report": "TSV отчет",
        "filter.active_default": "Фильтры: важность=Маркированные",
        "filter.active_prefix": "Фильтры: ",
        "filter.search_part": "поиск={search}",
        "filter.severity_part": "важность={value}",
        "filter.kind_part": "тип={value}",
        "filter.prime_only_part": "только 3' риски",
        "filter.showing": "Показано {shown} из {total} структур",
        "filter.showing_zero": "Показано 0 из 0 структур",
        "filter.search": "Поиск",
        "filter.severity": "Важность",
        "filter.type": "Тип",
        "filter.prime_only": "Только 3' риски",
        "heading.assessment": "Оценка",
        "heading.conc": "Конц. (нМ)",
        "heading.dg": "dG",
        "heading.dg_p3": "dG P3",
        "heading.dg_vienna": "dG Vienna",
        "heading.dg_rnastructure": "dG RNAstruct",
        "heading.tm_rnastructure": "Tm RNAstruct (P3 для димеров)",
        "heading.dg_seqfold": "dG seqfold",
        "heading.tm_seqfold": "Tm seqfold",
        "heading.flag": "Участок",
        "heading.gc": "GC %",
        "heading.kind": "Тип",
        "heading.label": "Метка",
        "heading.len": "Длина",
        "heading.name": "Имя",
        "heading.n": "n",
        "heading.oligo": "Олигонуклеотид",
        "heading.role": "Роль",
        "heading.sequence": "Последовательность (5'->3')",
        "heading.severity": "Важность",
        "heading.rank": "Ранг",
        "heading.discovered_by": "Найдено движками",
        "heading.structure": "Классифицированная структура",
        "heading.tm": "Tm",
        "heading.tm_c": "Tm (C)",
        "heading.tm_engines": "Tm по движкам (C)",
        "heading.tm_owcz": "Tm Owcz",
        "heading.tm_sl": "Tm SL",
        "heading.variants": "Варианты",
        "input.bulk_tab": "Пакетный ввод / мультиплекс (по одному на строку)",
        "input.forward": "Прямой праймер",
        "input.help": "Введите хотя бы один олигонуклеотид. Для одного олигонуклеотида показываются только шпильки и гомодимеры.",
        "input.probe1": "Зонд 1",
        "input.probe2": "Зонд 2",
        "input.reverse": "Обратный праймер",
        "input.single_tab": "Один анализ (F / R / зонды)",
        "label.language": "Язык",
        "matrix.empty": "Нет результатов по димерам. Запустите анализ, чтобы заполнить матрицу.",
        "matrix.legend": "** Проблема   ! Внимание   3' риск   Диагональ: гомодимер",
        "matrix.no_cells": "Нет ячеек димеров для просмотра.",
        "matrix.run_first": "Запустите анализ, чтобы заполнить матрицу.",
        "matrix.status_cell": "{label}: {severity}, dG {dg}; место #{rank}",
        "matrix.status_click": "Нажмите ячейку для просмотра",
        "matrix.show_flagged": "Только маркированные ячейки",
        "matrix.legend.problem": "** Проблема",
        "matrix.legend.caution": "! Внимание",
        "matrix.legend.prime": "3' риск",
        "matrix.legend.self": "Диаг. гомодимер",
        "option.all": "Все",
        "option.caution": "Внимание",
        "option.flagged": "Маркированные",
        "option.hairpin": "Шпилька",
        "option.hetero_dimer": "Гетеродимер",
        "option.ok": "OK",
        "option.problem": "Проблема",
        "option.self_dimer": "Гомодимер",
        "option.custom": "Пользовательские",
        "overview.cautions": "Внимание",
        "overview.looks_clean": "Выглядит чисто",
        "overview.no_flagged": "Главные риски: нет маркированных среди {count} олигонуклеотидов.",
        "overview.not_analyzed": "Не анализировано",
        "overview.prime_risks": "3' риски",
        "overview.problems": "Проблемы",
        "overview.review_cautions": "Только предупреждения",
        "overview.review_problems": "Проверьте проблемы",
        "overview.top_risks": "Главные риски: {items}",
        "overview.top_risks_idle": "Главные риски: запустите анализ вторичных структур.",
        "overview.risk_item": "{label} | {severity} | {site} | место #{rank}",
        "overview.more": "+{count} ещё",
        "overview.verdict": "Вердикт",
        "overview.worst": "Классифицированные структуры",
        "progress.cancelled": "Отменено",
        "progress.cancelling": "Отмена после текущего шага...",
        "progress.complete": "Анализ завершен",
        "progress.failed": "Анализ не выполнен",
        "progress.idle": "Ожидание",
        "progress.starting": "Запуск анализа...",
        "progress.starting_estimate": (
            "Анализ выполняется: проверяются и инициализируются "
            "обязательные движки; загружаются и подготавливаются "
            "термодинамические модели. "
            "Контексты вариантов: {allocated}/{total}"),
        "progress.preparing": (
            "Анализ выполняется: проверяются и инициализируются "
            "обязательные движки; загружаются и подготавливаются "
            "термодинамические модели"),
        "run.title": "Запуск",
        "section.conditions": "Условия",
        "severity.caution": "ВНИМАНИЕ",
        "severity.ok": "OK",
        "severity.unclassified": "НЕ КЛАССИФИЦИРОВАНО",
        "severity.problem": "ПРОБЛЕМА",
        "site.3prime": "3' конец",
        "site.internal": "внутри",
        "status.analysis_cancelled": "Анализ отменен.{tail}",
        "status.analysis_failed": (
            "Анализ не выполнен. Исправьте указанную причину и снова нажмите «Анализ»."
            "{tail}"),
        "status.analysis_running": "Анализ идет... Предыдущие результаты остаются видимыми до завершения.",
        "status.core_complete": "Результаты Primer3/ViennaRNA готовы; расчеты RNAstructure/seqfold продолжаются (экспорт пока недоступен).",
        "status.external_pending": "Ожидаются расчеты RNAstructure/seqfold",
        "status.cancelled_tail": " Предыдущие результаты сохранены.",
        "status.enter_sequences": "Введите последовательности и нажмите Анализ.",
        "status.external_idle": "Встроенные движки (Primer3, ViennaRNA, RNAstructure, seqfold): проверяются при запуске анализа.",
        "status.external_health": "Состояние встроенных движков: {items}",
        "status.ensemble_complete": (
            "Дополнительный анализ вырожденных нуклеотидов выполнен: "
            "{evaluated}/{total}."),
        "status.ensemble_partial": (
            "Дополнительный анализ вырожденных нуклеотидов частично выполнен: "
            "{evaluated}/{total}. Пропущенные варианты могут содержать "
            "более сильные структуры; результат не является наихудшим "
            "для всего ансамбля."),
        "status.ensemble_failed": "ошибок: {count}",
        "status.engine_unavailable": "{engine} недоступен",
        "status.engine_interactions": "{engine}: {covered}/{total} взаимодействий",
        "status.no_liabilities": "Значимых рисков шпилек или димеров не обнаружено.",
        "status.no_problems": "Проблем нет, предупреждений: {cautions}. Проверьте маркированные структуры ниже.",
        "status.problems": "Проблем: {problems}, предупреждений: {cautions}. Проверьте маркированные структуры ниже.",
        "structure.header": (
            "{label}  ({role})\n"
            "dG(P3) = {dg:.2f}{vienna}{tm}   [{severity}, {site}]\n"
        ),
        "structure.header_fixed": (
            "{label}  ({role})\n"
            "dG(Vienna, фиксированная структура) = {dg:.2f}{tm}   "
            "[{severity}, {site}]\n"
        ),
        "structure.vienna": "   dG(Vienna) = {dg:.2f}",
        "structure.tm_both": "   Tm Primer3 = {tm_p3:.2f} C   Tm Vienna фиксированной структуры = {tm_vienna:.2f} C",
        "structure.tm_single": "   Tm Primer3 = {tm:.2f} C",
        "structure.tm_fixed": "   Tm при dG фиксированной структуры=0 = {tm:.2f} C",
        "structure.representative_variant": "репрезентативный из {count} комбинаций (охват движками кроме Primer3 неполный): {variant}\n",
        "structure.role.gap_policy_derived": "Получено по правилам пропусков",
        "tm.method.primer3": "P3, модель двух состояний",
        "tm.method.vienna": "Vienna, фиксированная структура",
        "tm.method.rnastructure_hairpin": "RNAstruct, фиксированная CT",
        "tm.method.rnastructure_dimer": "RNAstruct, модель P3",
        "tm.method.seqfold": "seqfold, термодинамика фиксированной структуры dH/dS",
        "metric_state.calculation_failed": "ошибка расчета",
        "metric_state.not_installed": "не установлен",
        "metric_state.not_reported": "не сообщено",
        "metric_state.not_run": "не запускался",
        "metric_state.no_sign_change": "нет смены знака",
        "metric_state.below_search_range": "ниже диапазона поиска",
        "metric_state.above_search_range": "выше диапазона поиска",
        "metric_state.path_changed": "структура изменилась",
        "metric_state.non_affine": "нелинейная зависимость",
        "metric_state.not_calculated": "не рассчитывалось",
        "metric_state.engine_error": "ошибка движка",
        "metric_state.parse_error": "ошибка разбора",
        "metric_state.unsupported": "не поддерживается",
        "unit.dg": "{value} ккал/моль",
        "unit.nt": "{n} нт",
        "bulk.default_name": "Олиго {index}",
        "oligo.forward": "Прямой праймер",
        "oligo.reverse": "Обратный праймер",
        "oligo.probe1": "Зонд 1",
        "oligo.probe2": "Зонд 2",
        "tab.flagged": "Маркированные структуры",
        "tab.hairpins": "Шпильки",
        "tab.hetero": "Гетеродимеры",
        "tab.matrix": "Матрица",
        "tab.self": "Гомодимеры",
        "tab.structures": "Структуры",
        "tab.tm": "Температуры плавления",
        "table.no_match": "Нет структур для этих фильтров.",
        "table.no_match_hint": "Сбросьте фильтры, чтобы показать все результаты.",
        "action.copy_rows": "Копировать выбранные строки",
        "status.engine_ready": "Движки готовы",
        "preset.unsaved": "Текущие условия содержат несохранённые изменения.",
        "preset.saved_inline": "Параметры сохранены: {name}",
        "preset.deleted_inline": "Параметры удалены: {name}",
        "archive.preview_title": "Проверка архива анализа",
        "archive.preview_heading": "Импортировать этот завершённый анализ?",
        "archive.preview_body": (
            "Олигонуклеотиды: {oligos}\nУсловия: {conditions}\n"
            "Дополнительный анализ: {mode}; лимит {budget}\n"
            "Охват: {evaluated}/{total} ({completeness})\n"
            "Версия-источник: {version}\nНаучная политика: {policy}\n"
            "ID анализа: {analysis_id}"),
        "archive.preview_historical_policy": (
            "Этот архив использует распознанную историческую научную политику. "
            "Сохранённые результаты будут восстановлены без изменений и не будут "
            "пересчитаны по текущей политике."),
        "archive.preview_complete": "полный",
        "archive.preview_partial": "частичный",
        "archive.confirm_import": "Импортировать",
        "validation.bulk_empty": "Введите хотя бы один олигонуклеотид (по одному на строку) во вкладке пакетного ввода / мультиплекс.",
    },
}
_OPTION_KEYS = {
    "Flagged": "option.flagged",
    "All": "option.all",
    "Problem": "option.problem",
    "Caution": "option.caution",
    "OK": "option.ok",
    "Hairpin": "option.hairpin",
    "Self-dimer": "option.self_dimer",
    "Hetero-dimer": "option.hetero_dimer",
}
_KIND_KEYS = {
    "Hairpin": "option.hairpin",
    "Self-dimer": "option.self_dimer",
    "Hetero-dimer": "option.hetero_dimer",
}
_COND_FIELDS = [
    ("Na+/K+ (mM)", "mv_conc"),
    ("Mg2+ (mM)", "dv_conc"),
    ("dNTPs (mM)", "dntp_conc"),
    ("Primer (nM)", "primer_conc"),
    ("Probe (nM)", "probe_conc"),
    ("dG temp (C)", "dg_temp_c"),
    ("dG caution", "dg_caution"),
    ("dG problem", "dg_problem"),
    ("Near-duplicate bond difference (<n)",
     "near_duplicate_bond_difference"),
    ("Dimer max consecutive gaps", "dimer_max_consecutive_gaps"),
    ("Dimer max total gaps", "dimer_max_total_gaps"),
]
_COND_LABELS = {key: label for label, key in _COND_FIELDS}
_INTEGER_CONDITION_FIELDS = {
    "near_duplicate_bond_difference",
    "dimer_max_consecutive_gaps",
    "dimer_max_total_gaps",
}
_COND_PRESETS = {
    "qPCR / TaqMan": te.ReactionConditions(),
}
_COND_PRESET_NAMES = tuple(_COND_PRESETS) + ("Custom",)
_SELF_TEST_EXPORT_TEXT = (
    "dG=-10.25 kcal/mol | Tm=62.5 \N{DEGREE SIGN}C | n=6 | "
    "\u043a\u043a\u0430\u043b/\u043c\u043e\u043b\u044c | \u0421\u0442\u0440\u0443\u043a\u0442\u0443\u0440\u0430")


def _tr(lang: str, key: str, **values) -> str:
    text = _I18N.get(lang, _I18N["en"]).get(key, _I18N["en"].get(key, key))
    return text.format(**values) if values else text


def _show_rules_compat_message(title: str, body: str) -> None:
    """Support non-Tk projection probes; the real UI always uses a Toplevel."""

    messagebox.showinfo(title, body)


def frozen_self_test(root: tk.Tk | None = None,
                     progress=None) -> dict[str, str]:
    """Run real Tk and mandatory-engine smoke calculations for a package."""

    owns_root = root is None
    if root is None:
        root = tk.Tk()
        root.withdraw()
    try:
        result = _frozen_self_test_with_root(root, progress)
        seqfold_thermo = te.ee._seqfold_hairpin_thermo(
            "GGGAAACCC", te.ReactionConditions().dg_temp_c)
        if (
            seqfold_thermo.status != "crossing_found"
            or seqfold_thermo.tm_c is None
            or not math.isfinite(seqfold_thermo.tm_c)
        ):
            raise RuntimeError(
                "seqfold source-model thermodynamics smoke calculation failed")
        return result
    finally:
        if owns_root:
            root.destroy()


def _self_test_progress(progress, stage: str) -> None:
    if progress is not None:
        progress(stage)


def _self_test_primer3(
    cond: te.ReactionConditions, sequence: str, partner: str,
) -> None:
    primer3_tm = te.calc_tm(
        te.Oligo("Self test", sequence, "primer", cond.primer_conc), cond)
    if not math.isfinite(primer3_tm.tm_mean):
        raise RuntimeError("Primer3 smoke calculation returned no finite Tm")
    common = {
        "mv_conc": cond.mv_conc, "dv_conc": cond.dv_conc,
        "dntp_conc": cond.dntp_conc, "dna_conc": cond.primer_conc,
        "temp_c": cond.dg_temp_c,
    }
    results = {
        "hairpin": te.primer3.calc_hairpin(sequence, **common),
        "self-dimer": te.primer3.calc_homodimer(sequence, **common),
        "heterodimer": te.primer3.calc_heterodimer(
            sequence, partner, **common),
    }
    for label, result in results.items():
        if (not bool(result.structure_found)
                or not math.isfinite(float(result.dg))
                or not math.isfinite(float(result.tm))):
            raise RuntimeError(
                f"Primer3 {label} smoke calculation was not finite/complete")


def _self_test_exports(figure_type) -> None:
    import warnings

    export_figure = figure_type(figsize=(1, 1))
    export_figure.add_subplot(111).plot((0.0, 1.0), (0.0, 1.0))
    export_figure.axes[0].set_title(_SELF_TEST_EXPORT_TEXT)
    export_signatures = {
        "png": lambda payload: payload.startswith(b"\x89PNG"),
        "pdf": lambda payload: payload.startswith(b"%PDF"),
        "svg": lambda payload: b"<svg" in payload[:1024],
    }
    for export_format, is_valid in export_signatures.items():
        output = BytesIO()
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            export_figure.savefig(output, format=export_format)
        missing = [warning for warning in captured
                   if "missing from font" in str(warning.message)]
        if missing:
            raise RuntimeError(
                f"Matplotlib {export_format.upper()} export has missing glyphs: "
                + "; ".join(str(warning.message) for warning in missing))
        if not is_valid(output.getvalue()):
            raise RuntimeError(
                f"Matplotlib {export_format.upper()} export smoke test failed")


def _self_test_vienna(
    cond: te.ReactionConditions, sequence: str, partner: str,
) -> None:
    if not vb.available():
        raise RuntimeError("ViennaRNA/RNA backend is not available")
    vb.require_operational(cond)
    vienna_energy = vb.hairpin_dg(sequence, cond)
    if vienna_energy is None or not math.isfinite(vienna_energy):
        raise RuntimeError("ViennaRNA smoke calculation returned no finite fold")
    dimer_energy = vb.dimer_dg(sequence, partner, cond)
    structures = vb.discover_dimer_structures(sequence, partner, cond)
    if dimer_energy is None or not math.isfinite(dimer_energy) or not structures:
        raise RuntimeError("ViennaRNA dimer smoke calculation failed")
    fixed_energy = vb.dimer_structure_dg(
        sequence, partner, structures[0].pairs, cond)
    if fixed_energy is None or not math.isfinite(fixed_energy):
        raise RuntimeError("ViennaRNA fixed-dimer smoke calculation failed")


def _self_test_external_engines(
    cond: te.ReactionConditions, sequence: str, partner: str,
) -> str:
    external_batch = te.ee.run_hairpin_engines(sequence, cond)
    statuses = dict(external_batch.statuses)
    for engine in ("RNAstructure", "seqfold"):
        if statuses.get(engine) != "ok":
            raise RuntimeError(
                f"{engine} smoke calculation failed: "
                f"{statuses.get(engine, 'missing')}")
        if not any(
                observation.engine == engine
                and observation.status == "ok"
                and observation.dg_kcal_mol is not None
                and math.isfinite(observation.dg_kcal_mol)
                for observation in external_batch.observations):
            raise RuntimeError(f"{engine} smoke calculation returned no finite fold")
    for label, left, right in (
        ("self-dimer", sequence, sequence),
        ("heterodimer", sequence, partner),
    ):
        batch = te.ee.run_dimer_engines(
            left, right, cond, self_dimer=left == right)
        if (dict(batch.statuses).get("RNAstructure") != "ok"
                or not any(
                    item.engine == "RNAstructure" and item.status == "ok"
                    and item.dg_kcal_mol is not None
                    and math.isfinite(item.dg_kcal_mol)
                    for item in batch.observations)):
            raise RuntimeError(
                f"RNAstructure {label} smoke calculation failed")
    seqfold_version = te.ee.discover_external_engines()["seqfold"].version
    return seqfold_version


def _frozen_self_test_with_root(root: tk.Tk, progress=None) -> dict[str, str]:
    """Self-test implementation after the real Tk root is known to exist."""

    if not isinstance(root, tk.Tk) or not root.winfo_exists():
        raise RuntimeError("self-test requires the live Tk root created at startup")
    tk_version = str(root.tk.call("info", "patchlevel"))
    root.update_idletasks()
    _self_test_progress(progress, "tk")
    validate_cascadia_mono(root)
    _self_test_progress(progress, "font")
    mandatory = te.ee.require_mandatory_engines()
    primer3_identity = te.sm.validated_primer3_runtime_identity(te.primer3)
    vienna_identity = te.sm.validated_vienna_runtime_identity()
    _self_test_progress(progress, "matplotlib")
    from matplotlib.backends import backend_tkagg  # noqa: F401
    from matplotlib.figure import Figure

    _self_test_progress(progress, "primer3")
    cond = te.ReactionConditions()
    seq = te.clean_sequence("GCGCAAAAGCGC")
    partner = te.clean_sequence("GCGCTTTTGCGC")
    _self_test_primer3(cond, seq, partner)
    _self_test_exports(Figure)
    _self_test_progress(progress, "exports")
    _self_test_progress(progress, "vienna")
    _self_test_vienna(cond, seq, partner)
    _self_test_progress(progress, "rnastructure")
    seqfold_version = _self_test_external_engines(cond, seq, partner)
    _self_test_progress(progress, "seqfold")
    archive_identity = analyzed_run_archive.packaged_archive_self_test()
    _self_test_progress(progress, "archive")
    manifest = te.create_scientific_manifest([], cond)
    result = {
        "application": manifest.application_version,
        "scientific_policy": manifest.scientific_policy_version,
        "python": manifest.python_version,
        "tk": tk_version,
        "font": CASCADIA_MONO_NAME,
        "primer3_py": manifest.primer3_py_version,
        "primer3_core": primer3_identity["libprimer3_version"],
        "primer3_target": primer3_identity["target"],
        "primer3_thermoanalysis": primer3_identity[
            "thermoanalysis_filename"],
        "primer3_thermoanalysis_sha256": primer3_identity[
            "thermoanalysis_sha256"],
        "primer3_p3helpers": primer3_identity["p3helpers_filename"],
        "primer3_p3helpers_sha256": primer3_identity["p3helpers_sha256"],
        "matplotlib": "ok",
        "diagram_exports": "png,pdf,svg",
        "primer3": "ok",
        "vienna": vb.version() or "available",
        "vienna_target": vienna_identity["target"],
        "vienna_native": vienna_identity["native_binding_filename"],
        "vienna_native_sha256": vienna_identity["native_binding_sha256"],
        "seqfold": seqfold_version,
        "rnastructure_native": str(
            mandatory["RNAstructure"].get("native_module_sha256", "")),
        "vienna_parameters": manifest.vienna_parameter_set,
        "source_tree_sha256": manifest.source_tree_hash,
        "dependency_lock_sha256": manifest.dependency_lock_hash,
        "platform": f"{manifest.operating_system} {manifest.architecture}",
        "build_date": manifest.build_date or "source-install",
        "artifact_sha256": manifest.release_artifact_hash or "source-install",
    }
    result.update(archive_identity)
    return result


class MBUprimeStructLabApp(ttk.Frame):
    def __init__(self, master: tk.Tk):
        super().__init__(master, padding=_SPACE["md"], style="Workbench.TFrame")
        self.master = master
        apply_window_icon(master)
        self.grid(sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self._configure_styles()

        self._seq_vars: dict[str, tk.StringVar] = {}
        self._entries: dict[str, ttk.Entry] = {}
        self._cond_vars: dict[str, tk.StringVar] = {}
        self._condition_summary_var = tk.StringVar(value="")
        self._condition_preset_var = tk.StringVar(value="qPCR / TaqMan")
        self._condition_preset_combo: ttk.Combobox | None = None
        self._save_preset_btn: ttk.Button | None = None
        self._delete_preset_btn: ttk.Button | None = None
        self._preset_status_var = tk.StringVar(value="")
        self._custom_condition_presets: dict[str, te.ReactionConditions] = {}
        self._preset_load_error: Exception | None = None
        try:
            self._custom_condition_presets = gui_exports.load_condition_presets()
        except gui_exports.PersistenceError as error:
            self._preset_load_error = error
        self._sequence_length_labels: list[tuple[tk.StringVar, ttk.Label]] = []
        self._language_code = "en"
        self._language_var = tk.StringVar(value=_LANGUAGE_NAMES["en"])
        self._ensemble_complete_var = tk.BooleanVar(value=True)
        self._ensemble_budget_var = tk.StringVar(
            value=str(te.DEFAULT_ENSEMBLE_ADDITIONAL_BUDGET))
        self._additional_analysis_expanded = (
            DEFAULT_ADDITIONAL_ANALYSIS_EXPANDED)
        self._additional_analysis_summary_var = tk.StringVar(value="")
        self._additional_analysis_details: ttk.Frame | None = None
        self._additional_analysis_toggle_btn: ttk.Button | None = None
        self._localized_widgets: list[tuple[object, str, str]] = []
        self._localized_tabs: list[tuple[ttk.Notebook, object, str]] = []
        self._localized_tree_headings: list[tuple[ttk.Treeview, str, str]] = []
        self._localized_menus: list[tuple[tk.Menu, list[tuple[int, str]]]] = []
        self._localized_filter_combos: dict[str, ttk.Combobox] = {}
        self._condition_details: ttk.Frame | None = None
        self._condition_toggle_btn: ttk.Button | None = None
        self._conditions_expanded = False
        self._updating_conditions = False
        self._last_report: te.AnalysisReport | None = None
        self._last_oligos: list[te.Oligo] | None = None
        self._last_cond: te.ReactionConditions | None = None
        self._panels: dict[str, dict] = {}
        self._problems_panel: dict | None = None
        self._matrix_panel: dict | None = None
        self._filter_vars: dict[str, tk.StringVar] = {}
        self._prime_risks_only_var = tk.BooleanVar(value=False)
        self._matrix_flagged_only_var = tk.BooleanVar(value=False)
        self._problems_sort_column: str | None = None
        self._problems_sort_desc = True
        self._suspend_problem_refresh = False
        self._overview_vars: dict[str, tk.StringVar] = {}
        self._top_risks_var = tk.StringVar(value=_tr("ru", "overview.top_risks_idle"))
        self._bulk_preview_tree: ttk.Treeview | None = None
        self._setup_pane: ttk.Frame | None = None
        self._workspace_pane: ttk.Frame | None = None
        self._detail_frame: ttk.LabelFrame | None = None
        self._detail_text: tk.Text | None = None
        self._detail_title_var = tk.StringVar(value=_tr("ru", "detail.none_selected"))
        self._detail_status_var = tk.StringVar(value="")
        self._detail_diagram_btn: ttk.Button | None = None
        self._language_combo: ttk.Combobox | None = None
        self._detail_structure = None
        self._matrix_cell_map: dict[tuple[int, int], te.ReportFinding] = {}
        self._matrix_rects: dict[tuple[int, int], int] = {}
        self._matrix_selected: tuple[int, int] | None = None
        self._analysis_thread: threading.Thread | None = None
        self._analysis_queue: queue.Queue | None = None
        self._cancel_event: threading.Event | None = None
        self._analysis_pending = False
        self._progress_indeterminate = False
        self._progress_has_completed_work = False
        self._progress_cancel_requested = False
        self._last_analysis_progress = None
        self._populate_generation = 0
        self._import_thread = None
        self._import_cancel = None
        self._import_generation = 0
        self._closing = False
        self._render_jobs = {}
        self.master.protocol("WM_DELETE_WINDOW", self._request_close)

        self._build_shell()
        self._build_inputs()
        self._build_conditions()
        self._build_actions()
        self._build_results()
        self._apply_language()
        if self._preset_load_error is not None:
            self.after_idle(self._show_preset_load_error)

    def _t(self, key: str, **values) -> str:
        return _tr(self._language_code, key, **values)

    def _bind_global_navigation(self) -> None:
        self.master.bind("<Control-Tab>",
                         lambda _event: self._cycle_results_tab(1), add="+")
        self.master.bind("<Control-Shift-Tab>",
                         lambda _event: self._cycle_results_tab(-1), add="+")

    def _cycle_results_tab(self, step: int):
        tabs = self.results_nb.tabs()
        if not tabs:
            return "break"
        current = self.results_nb.index(self.results_nb.select())
        self.results_nb.select(tabs[(current + step) % len(tabs)])
        self.results_nb.focus_set()
        return "break"

    def _text(self, widget, key: str, option: str = "text"):
        widget.configure(**{option: self._t(key)})
        self._localized_widgets.append((widget, option, key))
        return widget

    def _tab(self, notebook: ttk.Notebook, child, key: str):
        notebook.tab(child, text=self._t(key))
        self._localized_tabs.append((notebook, child, key))

    def _heading(self, tree: ttk.Treeview, column: str, key: str,
                 command=None):
        options = {"text": self._t(key)}
        if command is not None:
            options["command"] = command
        tree.heading(column, **options)
        self._localized_tree_headings.append((tree, column, key))

    def _display_option(self, canonical: str) -> str:
        return self._t(_OPTION_KEYS.get(canonical, canonical))

    def _canonical_filter_value(self, key: str) -> str:
        value = self._filter_vars[key].get()
        if value in _FILTER_OPTIONS[key]:
            return value
        for canonical in _FILTER_OPTIONS[key]:
            if value in {_tr("en", _OPTION_KEYS[canonical]),
                         _tr("ru", _OPTION_KEYS[canonical])}:
                return canonical
        return _FILTER_OPTIONS[key][0]

    def _display_kind(self, kind: str) -> str:
        return self._t(_KIND_KEYS.get(kind, kind))

    def _display_oligo_name(self, name: str) -> str:
        text = str(name)
        match = re.fullmatch(r"Oligo (\d+)", text)
        if match:
            return self._t("bulk.default_name", index=match.group(1))
        keys = {
            "Forward primer": "oligo.forward",
            "Reverse primer": "oligo.reverse",
            "Probe 1": "oligo.probe1",
            "Probe 2": "oligo.probe2",
        }
        return self._t(keys[text]) if text in keys else text

    def _display_structure_label(self, label: str, kind: str = "") -> str:
        """Localize canonical GUI labels without changing report identity."""
        text = str(label)
        canonical_kind = kind if kind in _KIND_KEYS else ""
        if not canonical_kind:
            canonical_kind = next(
                (candidate for candidate in _KIND_KEYS
                 if text.startswith(candidate + ":")), "")
        if canonical_kind and text.startswith(canonical_kind + ":"):
            text = self._display_kind(canonical_kind) + text[len(canonical_kind):]
        for canonical_name in (
                "Forward primer", "Reverse primer", "Probe 1", "Probe 2"):
            text = text.replace(
                canonical_name, self._display_oligo_name(canonical_name))
        return re.sub(
            r"(?<![A-Za-z])Oligo (\d+)",
            lambda match: self._t("bulk.default_name", index=match.group(1)),
            text,
        )

    def _display_structure_role(self, role: str) -> str:
        if role == "Gap-policy derived":
            return self._t("structure.role.gap_policy_derived")
        return role

    def _display_progress_message(self, message: str) -> str:
        if message == "Preparing analysis":
            return self._t("progress.preparing")
        for kind in _KIND_KEYS:
            prefix = kind + ": "
            if message.startswith(prefix):
                name = self._display_oligo_name(message[len(prefix):])
                return f"{self._display_kind(kind)}: {name}"
        if message.startswith("Tm: "):
            return "Tm: " + self._display_oligo_name(message[4:])
        return message

    def _display_condition_preset(self, name: str) -> str:
        return self._t("option.custom") if name == "Custom" else name

    def _condition_preset_names(self) -> tuple[str, ...]:
        custom = tuple(sorted(self._custom_condition_presets, key=str.casefold))
        return tuple(_COND_PRESETS) + custom + ("Custom",)

    def _all_condition_presets(self) -> dict[str, te.ReactionConditions]:
        return {**_COND_PRESETS, **self._custom_condition_presets}

    @staticmethod
    def _canonical_condition_preset(value: str) -> str:
        if value in {_tr("en", "option.custom"),
                     _tr("ru", "option.custom")}:
            return "Custom"
        return value

    def _display_site(self, involves_3prime: bool) -> str:
        return self._t("site.3prime" if involves_3prime else "site.internal")

    def _display_severity(self, severity: str) -> str:
        return self._t(f"severity.{severity}")

    def _display_assessment(self, assessment: str, kind: str = "") -> str:
        if self._language_code != "ru" or not assessment:
            return assessment
        is_dimer = kind in {"Self-dimer", "Hetero-dimer"}
        if is_dimer:
            very_stable = "Очень стабильный"
            moderately_stable = "Умеренно стабильный"
            weak = "Слабый"
        else:
            very_stable = "Очень стабильная"
            moderately_stable = "Умеренно стабильная"
            weak = "Слабая"
        text = assessment
        hairpin_tm_replacements = [
            ("Weak stem/Tm", "Слабое основание/Tm"),
            ("Moderately stable stem/Tm", "Умеренно стабильное основание/Tm"),
            ("Very stable stem/Tm", "Очень стабильное основание/Tm"),
        ]
        for source, target in hairpin_tm_replacements:
            text = text.replace(source, target)
        replacements = [
            ("no finite exact-geometry dG",
             "нет доступного конечного dG точной геометрии"),
            ("exact-geometry Tm unavailable",
             "Tm точной геометрии недоступна"),
            ("fixed-structure Tm unavailable",
             "Tm фиксированной структуры недоступна"),
            ("non-Primer3 engines cover the Primer3-selected variant only",
             "движки кроме Primer3 охватывают только выбранный Primer3 вариант"),
            ("representative of", "репрезентативный для"),
            ("not classified", "не классифицировано"),
            ("paired bases", "пар оснований"),
            ("stem has", "основание содержит"),
            ("No stable structure detected", "Стабильная структура не обнаружена"),
            ("Very stable", very_stable),
            ("Moderately stable", moderately_stable),
            ("Weak", weak),
            ("min dG", "мин dG"),
            ("max Tm", "макс Tm"),
            ("kcal/mol", "ккал/моль"),
            ("3' end", "3' конец"),
            ("internal", "внутри"),
            ("likely to interfere with PCR", "может помешать ПЦР"),
            ("acceptable", "приемлемо"),
            ("monitor", "проверьте"),
            ("hairpin Tm Primer3", "Tm шпильки Primer3"),
            ("Vienna fixed structure", "Vienna фиксированной структуры"),
            ("hairpin Tm", "Tm шпильки"),
            ("stem/Tm", "основание/Tm"),
            ("with", "при"),
            ("base pairs", "пар оснований"),
            ("worst", "худшее"),
            ("variant combinations", "комбинаций вариантов"),
        ]
        for source, target in replacements:
            text = text.replace(source, target)
        return text

    def _set_language_from_var(self, _event=None):
        self._set_language(_LANGUAGE_BY_NAME.get(
            self._language_var.get(), "en"))

    def _set_language(self, lang: str):
        if lang not in _I18N:
            lang = "en"
        self._language_code = lang
        self._language_var.set(_LANGUAGE_NAMES[lang])
        self._apply_language()

    def _apply_language(self):
        self._apply_localized_labels()
        self._apply_localized_controls()
        self._apply_language_layout()
        self._apply_localized_summaries()
        self._apply_localized_results()

    def _apply_localized_labels(self):
        """Project translated text onto registered widgets and menus."""

        for widget, option, key in self._localized_widgets:
            try:
                widget.configure(**{option: self._t(key)})
            except tk.TclError:
                pass
        for notebook, child, key in self._localized_tabs:
            try:
                notebook.tab(child, text=self._t(key))
            except tk.TclError:
                pass
        for tree, column, key in self._localized_tree_headings:
            try:
                tree.heading(column, text=self._t(key))
            except tk.TclError:
                pass
        for menu, entries in self._localized_menus:
            for index, key in entries:
                try:
                    menu.entryconfigure(index, label=self._t(key))
                except tk.TclError:
                    pass

    def _apply_localized_controls(self):
        """Refresh translated filter/preset values and sequence lengths."""

        for key, combo in self._localized_filter_combos.items():
            canonical = self._canonical_filter_value(key)
            combo.configure(values=tuple(
                self._display_option(v) for v in _FILTER_OPTIONS[key]))
            self._filter_vars[key].set(self._display_option(canonical))
        if self._condition_preset_combo is not None:
            canonical = self._canonical_condition_preset(
                self._condition_preset_var.get())
            self._condition_preset_combo.configure(values=tuple(
                self._display_condition_preset(name)
                for name in self._condition_preset_names()))
            self._condition_preset_var.set(
                self._display_condition_preset(canonical))
        for variable, label in self._sequence_length_labels:
            self._update_len(variable, label)

    def _apply_localized_summaries(self):
        """Refresh translated disclosure controls and summary projections."""

        if self._condition_toggle_btn is not None:
            self._condition_toggle_btn.config(
                text=self._t("action.hide_details" if self._conditions_expanded
                             else "action.show_details"))
        if self._additional_analysis_toggle_btn is not None:
            self._additional_analysis_toggle_btn.config(
                text=self._t(
                    "action.collapse" if self._additional_analysis_expanded
                    else "action.expand"))
        self._update_condition_summary()
        self._update_preset_action_state()
        self._update_additional_analysis_summary()
        self._update_problem_sort_ui()
        if hasattr(self, "bulk_count"):
            self._update_bulk_count()

    def _apply_localized_results(self):
        """Translate results without replacing the active analysis status."""

        if self._last_report and self._last_oligos:
            self._populate(self._last_oligos, self._last_report)
        else:
            self._reset_overview()
            self._clear_result_tables()
            self._set_summary(self._t("status.enter_sequences"), "idle")
            if hasattr(self, "progress_label"):
                self.progress_label.config(text=self._t("progress.idle"))

        if getattr(self, "_analysis_pending", False):
            if getattr(self, "_progress_cancel_requested", False):
                self.progress_label.config(text=self._t("progress.cancelling"))
                self._set_summary(self._t("progress.cancelling"), "info")
            else:
                self._set_summary(self._t(
                    "status.analysis_running_previous" if self._last_report
                    else "status.analysis_running_empty"), "info")
                progress = getattr(self, "_last_analysis_progress", None)
                if progress is not None:
                    self._show_progress(progress)
                else:
                    counts = getattr(self, "_progress_start_counts", None)
                    text = (self._t("progress.starting_estimate",
                                    allocated=counts[0], total=counts[1])
                            if counts else self._t("progress.starting"))
                    self.progress_label.config(text=text)
        if getattr(self, "_import_thread", None) is not None:
            self.progress_label.config(text=self._t(
                "progress.cancelling" if self._import_cancel.is_set()
                else "archive.open_title"))

    def _apply_language_layout(self):
        """Adjust fixed Tk widget widths for translated labels."""
        ru = self._language_code == "ru"
        if hasattr(self, "tm_tree"):
            widths = {
                "en": {
                    "oligo": 115, "len": 42, "var": 62, "gc": 70,
                    "conc": 72, "tm": 82, "tm_owcz": 82, "seq": 300,
                },
                "ru": {
                    "oligo": 142, "len": 58, "var": 76, "gc": 70,
                    "conc": 88, "tm": 82, "tm_owcz": 82, "seq": 360,
                },
            }["ru" if ru else "en"]
            for column, width in widths.items():
                self.tm_tree.column(column, width=width)
        # Large old-language tables defer sizing and are superseded by the
        # translated population; empty/small tables retain immediate headings.
        panels = [(self._problems_panel, _FLAGGED_COLUMN_BOUNDS)] + [
            (panel, _STRUCTURE_COLUMN_BOUNDS)
            for panel in getattr(self, "_panels", {}).values()]
        for panel, bounds in panels:
            if panel is not None:
                self._run_render_steps(
                    f"layout-{id(panel)}", _autosize_tree_steps(
                        panel["tree"], bounds), len(panel.get("iid", {})))
        if self._language_combo is not None:
            self._language_combo.configure(width=12)

    def _configure_styles(self):
        gui_styles.configure_styles(self.master)

    def _configure_menu(self, menu: tk.Menu):
        gui_styles.configure_menu(menu)

    @staticmethod
    def _bind_bounded_wrap(widget, *, minimum: int, inset: int) -> None:
        """Keep long localized status text inside its owning widget."""

        widget.configure(wraplength=minimum, justify="left")

        def resize(event):
            if event.width > inset:
                wraplength = event.width - inset
                if int(widget.cget("wraplength")) != wraplength:
                    widget.configure(wraplength=wraplength)

        widget.bind("<Configure>", resize, add="+")

    def _build_shell(self):
        workbench = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        workbench.grid(row=0, column=0, sticky="nsew")

        self._setup_pane = ttk.Frame(
            workbench, width=_DIMEN["sidebar_width"],
            padding=(0, 0, _SPACE["md"], 0),
            style="Rail.TFrame")
        self._setup_pane.columnconfigure(0, weight=1)
        self._setup_pane.rowconfigure(0, weight=1)

        self._workspace_pane = ttk.Frame(
            workbench, padding=(_SPACE["md"], 0, 0, 0),
            style="Workspace.TFrame")
        self._workspace_pane.columnconfigure(0, weight=1)
        self._workspace_pane.rowconfigure(0, weight=1)

        workbench.add(self._setup_pane, weight=0)
        workbench.add(self._workspace_pane, weight=1)

    # ------------------------------------------------------------------ #
    # Input widgets
    # ------------------------------------------------------------------ #
    def _build_inputs(self):
        parent = self._setup_pane
        self.input_nb = ttk.Notebook(parent, style="Lab.TNotebook")
        self.input_nb.grid(row=0, column=0, sticky="nsew",
                           pady=(0, _SPACE["sm"]))

        # --- Tab 1: single assay (fixed F / R / probe fields) -------------- #
        frame = ttk.Frame(self.input_nb, padding=_SPACE["sm"], style="Lab.TFrame")
        self.input_nb.add(frame)
        self._tab(self.input_nb, frame, "input.single_tab")
        frame.columnconfigure(1, weight=1)

        rows = [
            ("input.forward", "fwd"),
            ("input.reverse", "rev"),
            ("input.probe1", "probe1"),
            ("input.probe2", "probe2"),
        ]
        for i, (label, key) in enumerate(rows):
            self._text(ttk.Label(frame, style="Lab.TLabel"), label).grid(
                row=i, column=0, sticky="w",
                padx=(0, _SPACE["sm"]), pady=_SPACE["xxs"])
            var = tk.StringVar()
            self._seq_vars[key] = var
            entry = ttk.Entry(frame, textvariable=var, font=_FONT["mono"],
                              style="Lab.TEntry")
            entry.grid(row=i, column=1, sticky="ew", pady=_SPACE["xxs"])
            self._entries[key] = entry
            self._attach_context_menu(entry)
            lenlbl = ttk.Label(frame, text=self._t("unit.nt", n=0),
                               width=8, anchor="e",
                               style="Lab.TLabel")
            lenlbl.grid(row=i, column=2, sticky="e", padx=(_SPACE["sm"], 0))
            self._sequence_length_labels.append((var, lenlbl))
            var.trace_add("write", lambda *_, v=var, l=lenlbl: self._update_len(v, l))

        self._text(ttk.Label(frame, style="Muted.TLabel", wraplength=330),
                   "input.help").grid(
            row=len(rows), column=0, columnspan=3, sticky="w",
            pady=(_SPACE["sm"], 0))

        # --- Tab 2: bulk / multiplex (one oligo per line) ----------------- #
        bulk = ttk.Frame(self.input_nb, padding=_SPACE["sm"], style="Lab.TFrame")
        self.input_nb.add(bulk)
        self._tab(self.input_nb, bulk, "input.bulk_tab")
        bulk.columnconfigure(0, weight=1)
        bulk.rowconfigure(1, weight=1)
        self._text(ttk.Label(bulk, style="Muted.TLabel", wraplength=330),
                   "bulk.help").grid(row=0, column=0, columnspan=2, sticky="w",
                                     pady=(0, _SPACE["xs"]))
        self.bulk_text = tk.Text(
            bulk, height=8, width=42, font=_FONT["mono"],
            wrap="none", undo=True, bg=_UI["surface"], fg=_UI["text"],
            insertbackground=_UI["text"], selectbackground=_UI["accent_soft"],
            relief="solid", borderwidth=1, highlightthickness=1,
            highlightbackground=_UI["border"], highlightcolor=_UI["focus"],
            padx=_SPACE["xs"], pady=_SPACE["xs"])
        self.bulk_text.grid(row=1, column=0, sticky="nsew")
        bsb = ttk.Scrollbar(
            bulk, orient="vertical", command=self.bulk_text.yview,
            style="Lab.Vertical.TScrollbar")
        bsb.grid(row=1, column=1, sticky="ns")
        self.bulk_text.configure(yscrollcommand=bsb.set)
        self._attach_text_menu(self.bulk_text)

        preview = ttk.LabelFrame(
            bulk, padding=_SPACE["xs"],
            style="Section.TLabelframe")
        self._text(preview, "bulk.preview")
        preview.grid(row=2, column=0, columnspan=2, sticky="ew",
                     pady=(_SPACE["sm"], 0))
        preview.columnconfigure(0, weight=1)
        pcols = ("name", "role", "len", "var", "conc")
        self._bulk_preview_tree = ttk.Treeview(
            preview, columns=pcols, show="headings", height=4,
            style="Lab.Treeview")
        for c, key, w, anc in [
            ("name", "heading.name", 150, "w"),
            ("role", "heading.role", 64, "w"),
            ("len", "heading.len", 44, "e"),
            ("var", "heading.variants", 66, "e"),
            ("conc", "heading.conc", 80, "e"),
        ]:
            self._heading(self._bulk_preview_tree, c, key)
            self._bulk_preview_tree.column(
                c, width=w, anchor=anc, stretch=(c == "name"))
        self._bulk_preview_tree.grid(row=0, column=0, sticky="ew")
        psb = ttk.Scrollbar(
            preview, orient="vertical", command=self._bulk_preview_tree.yview,
            style="Lab.Vertical.TScrollbar")
        psb.grid(row=0, column=1, sticky="ns")
        self._bulk_preview_tree.configure(yscrollcommand=psb.set)

        self.bulk_count = ttk.Label(bulk, style="Muted.TLabel")
        self._text(self.bulk_count, "bulk.zero")
        self.bulk_count.grid(row=3, column=0, columnspan=2, sticky="w",
                             pady=(_SPACE["xs"], 0))
        self.bulk_text.bind("<KeyRelease>", self._update_bulk_count)
        self.input_nb.select(bulk)

    def _using_bulk(self) -> bool:
        return self.input_nb.index(self.input_nb.select()) == 1

    def _condition_for_preview(self) -> te.ReactionConditions:
        try:
            return self._read_conditions()
        except te.SequenceError:
            return te.ReactionConditions()

    def _update_bulk_count(self, _event=None):
        text = self.bulk_text.get("1.0", "end")
        items = te.parse_bulk_oligos(text)
        n = len(items)
        pairs = n * (n - 1) // 2
        ignored = max(0, sum(1 for line in text.splitlines()
                             if line.strip()) - n)
        suffix = self._t("bulk.ignored_suffix", ignored=ignored) if ignored else ""
        self.bulk_count.config(
            text=self._t("bulk.count", n=n, pairs=pairs, suffix=suffix))
        self._refresh_bulk_preview(items)

    def _refresh_bulk_preview(self, items: list[tuple[str | None, str]]):
        tree = self._bulk_preview_tree
        if tree is None:
            return
        tree.delete(*tree.get_children())
        cond = self._condition_for_preview()
        for i, (label, raw) in enumerate(items, 1):
            name = label or self._t("bulk.default_name", index=i)
            is_probe = te.label_uses_probe_concentration(label)
            role = self._t("cond.label.probe_conc").split(" ")[0].lower() if is_probe else self._t("cond.label.primer_conc").split(" ")[0].lower()
            conc = cond.probe_conc if is_probe else cond.primer_conc
            try:
                seq = te.clean_sequence(raw)
                variants = te.expand_iupac(seq)
                length = len(seq)
                variant_count = len(variants)
            except te.SequenceError:
                length = len("".join(raw.split()))
                variant_count = "-"
            tree.insert("", "end", values=(
                name, role, length, variant_count, f"{conc:g}"))

    def _attach_context_menu(self, entry: ttk.Entry):
        """Right-click Cut/Copy/Paste/Select-all/Clear menu for an Entry."""
        self._attach_text_shortcuts(entry)
        menu = tk.Menu(entry, tearoff=0)
        self._configure_menu(menu)
        entries = []
        menu.add_command(label=self._t("action.cut"),
                         command=lambda: entry.event_generate("<<Cut>>"))
        entries.append((0, "action.cut"))
        menu.add_command(label=self._t("action.copy"),
                         command=lambda: entry.event_generate("<<Copy>>"))
        entries.append((1, "action.copy"))
        menu.add_command(label=self._t("action.paste"),
                         command=lambda: entry.event_generate("<<Paste>>"))
        entries.append((2, "action.paste"))
        menu.add_separator()
        menu.add_command(label=self._t("action.select_all"),
                         command=lambda: (entry.select_range(0, "end"),
                                          entry.icursor("end")))
        entries.append((4, "action.select_all"))
        menu.add_command(label=self._t("action.clear"),
                         command=lambda: entry.delete(0, "end"))
        entries.append((5, "action.clear"))
        self._localized_menus.append((menu, entries))

        def popup(event):
            entry.focus_set()
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

        entry.bind("<Button-3>", popup)

    def _attach_text_menu(self, widget: tk.Text):
        """Right-click Cut/Copy/Paste/Select-all/Clear menu for a Text widget."""
        self._attach_text_shortcuts(widget)
        menu = tk.Menu(widget, tearoff=0)
        self._configure_menu(menu)
        entries = []
        menu.add_command(label=self._t("action.cut"),
                         command=lambda: widget.event_generate("<<Cut>>"))
        entries.append((0, "action.cut"))
        menu.add_command(label=self._t("action.copy"),
                         command=lambda: widget.event_generate("<<Copy>>"))
        entries.append((1, "action.copy"))
        menu.add_command(label=self._t("action.paste"),
                         command=lambda: widget.event_generate("<<Paste>>"))
        entries.append((2, "action.paste"))
        menu.add_separator()
        menu.add_command(label=self._t("action.select_all"),
                         command=lambda: (widget.tag_add("sel", "1.0", "end"),
                                          "break"))
        entries.append((4, "action.select_all"))
        menu.add_command(label=self._t("action.clear"),
                         command=lambda: (widget.delete("1.0", "end"),
                                          self._update_bulk_count()))
        entries.append((5, "action.clear"))
        self._localized_menus.append((menu, entries))

        def popup(event):
            widget.focus_set()
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

        widget.bind("<Button-3>", popup)

    def _attach_text_shortcuts(self, widget):
        widget.bind("<Control-KeyPress>",
                    self._handle_layout_independent_shortcut)
        widget.bind("<Control-Shift-KeyPress>",
                    self._handle_layout_independent_shortcut)

    @staticmethod
    def _shortcut_action_for_event(event) -> str | None:
        state = getattr(event, "state", 0)
        keycode = getattr(event, "keycode", None)
        if not (state & _CTRL_MASK):
            return None
        if keycode == 90 and (state & _SHIFT_MASK):
            return "redo"
        return _LAYOUT_INDEPENDENT_SHORTCUTS.get(keycode)

    @staticmethod
    def _is_text_shortcut_widget(widget) -> bool:
        return isinstance(widget, (tk.Entry, ttk.Entry, tk.Text))

    @staticmethod
    def _widget_is_editable(widget) -> bool:
        try:
            if str(widget.cget("state")) in {"disabled", "readonly"}:
                return False
        except tk.TclError:
            return False
        if hasattr(widget, "state"):
            try:
                if {"disabled", "readonly"} & set(widget.state()):
                    return False
            except tk.TclError:
                return False
        return True

    def _handle_layout_independent_shortcut(self, event):
        action = self._shortcut_action_for_event(event)
        widget = getattr(event, "widget", None)
        if action is None or not self._is_text_shortcut_widget(widget):
            return None
        if action in _EDIT_SHORTCUTS and not self._widget_is_editable(widget):
            return "break"

        if action == "select_all":
            self._select_all_text_widget(widget)
        else:
            virtual_event = {
                "copy": "<<Copy>>",
                "cut": "<<Cut>>",
                "paste": "<<Paste>>",
                "undo": "<<Undo>>",
                "redo": "<<Redo>>",
            }[action]
            widget.event_generate(virtual_event)
            if action in _EDIT_SHORTCUTS:
                self._after_text_edit_shortcut(widget)
        return "break"

    def _select_all_text_widget(self, widget):
        if isinstance(widget, tk.Text):
            widget.tag_add("sel", "1.0", "end-1c")
            widget.mark_set("insert", "end-1c")
        else:
            widget.select_range(0, "end")
            widget.icursor("end")

    def _after_text_edit_shortcut(self, widget):
        if widget is getattr(self, "bulk_text", None):
            self.after_idle(self._update_bulk_count)

    def _update_len(self, var: tk.StringVar, label: ttk.Label):
        n = len("".join(var.get().split()))
        label.config(text=self._t("unit.nt", n=n))

    # ------------------------------------------------------------------ #
    # Conditions widgets
    # ------------------------------------------------------------------ #
    def _build_conditions(self):
        parent = self._setup_pane
        frame = ttk.LabelFrame(parent,
                               padding=_SPACE["sm"],
                               style="Section.TLabelframe")
        self._text(frame, "section.conditions")
        frame.grid(row=1, column=0, sticky="ew", pady=(0, _SPACE["sm"]))
        frame.columnconfigure(0, weight=1)

        top = ttk.Frame(frame, style="Lab.TFrame")
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)

        self._text(ttk.Label(top, style="Lab.TLabel"), "cond.preset").grid(
            row=0, column=0, sticky="w")
        preset = ttk.Combobox(
            top, textvariable=self._condition_preset_var,
            values=tuple(self._display_condition_preset(name)
                         for name in self._condition_preset_names()),
            state="readonly", width=16,
            style="Lab.TCombobox")
        self._condition_preset_combo = preset
        preset.grid(row=0, column=1, columnspan=2, sticky="ew",
                    padx=(_SPACE["xs"], 0))
        preset.bind("<<ComboboxSelected>>", self._on_condition_preset_selected)

        self._save_preset_btn = self._text(ttk.Button(
            top, style="Secondary.TButton", command=self._save_condition_preset),
            "action.save_preset")
        self._save_preset_btn.grid(
            row=1, column=1, sticky="ew",
            padx=(_SPACE["xs"], _SPACE["xxs"]), pady=(_SPACE["xs"], 0))
        self._delete_preset_btn = self._text(ttk.Button(
            top, style="Secondary.TButton", command=self._delete_condition_preset),
            "action.delete_preset")
        self._delete_preset_btn.grid(
            row=1, column=2, sticky="ew",
            padx=(_SPACE["xxs"], 0), pady=(_SPACE["xs"], 0))
        self._text(ttk.Button(top, style="Secondary.TButton",
                              command=self._reset_conditions),
                   "action.reset_defaults").grid(
            row=2, column=1, sticky="ew",
            padx=(_SPACE["xs"], _SPACE["xxs"]), pady=(_SPACE["xs"], 0))
        self._condition_toggle_btn = ttk.Button(
            top, command=self._toggle_conditions,
            style="Secondary.TButton")
        self._text(self._condition_toggle_btn, "action.show_details")
        self._condition_toggle_btn.grid(
            row=2, column=2, sticky="ew", padx=(_SPACE["xxs"], 0),
            pady=(_SPACE["xs"], 0))

        ttk.Label(
            frame, textvariable=self._condition_summary_var,
            style="Muted.TLabel", wraplength=340).grid(
            row=1, column=0, sticky="w", pady=(_SPACE["xs"], 0))
        ttk.Label(frame, textvariable=self._preset_status_var,
                  style="Status.TLabel", wraplength=340).grid(
            row=2, column=0, sticky="w", pady=(_SPACE["xxs"], 0))

        body = ttk.Frame(frame, style="Lab.TFrame")
        body.grid(row=3, column=0, sticky="ew", pady=(_SPACE["xs"], 0))
        self._condition_details = body

        # Two compact columns keep the fixed Run footer visible at 1020x660.
        d = _COND_PRESETS["qPCR / TaqMan"]
        columns = 2
        for column in range(columns):
            body.columnconfigure(column, weight=1)
        for idx, (_label, key) in enumerate(_COND_FIELDS):
            r, c = divmod(idx, columns)
            cell = ttk.Frame(body, style="Lab.TFrame")
            cell.grid(row=r, column=c, sticky="ew",
                      padx=_SPACE["xs"], pady=_SPACE["xs"])
            cell.columnconfigure(0, weight=1)
            self._text(ttk.Label(
                cell, anchor="w", justify="left", wraplength=110,
                style="Lab.TLabel"),
                       f"cond.label.{key}").grid(
                           row=0, column=0, sticky="ew",
                           padx=(0, _SPACE["xs"]))
            var = tk.StringVar(value=f"{getattr(d, key):g}")
            self._cond_vars[key] = var
            entry = ttk.Entry(cell, textvariable=var, width=8,
                              style="Lab.TEntry")
            entry.grid(row=0, column=1, sticky="e")
            self._attach_text_shortcuts(entry)
            topic = None
            if key in {"dg_caution", "dg_problem"}:
                topic = "dg_thresholds"
            elif key in {
                    "dimer_max_consecutive_gaps",
                    "dimer_max_total_gaps"}:
                topic = "dimer_gaps"
            elif key == "near_duplicate_bond_difference":
                topic = "near_duplicates"
            if topic is not None:
                help_button = ttk.Button(
                    cell, text="?", width=2, takefocus=True,
                    style="Secondary.TButton")
                help_button.configure(
                    command=lambda t=topic, b=help_button:
                    self._show_help_topic(t, b))
                help_button.grid(
                    row=0, column=2, sticky="e", padx=(_SPACE["xs"], 0))
            var.trace_add("write", self._on_condition_change)

        body.grid_remove()
        self._update_condition_summary()
        self._update_preset_action_state()

    def _show_preset_load_error(self):
        error = self._preset_load_error
        if error is None:
            return
        messagebox.showerror(
            self._t("preset.error_title"),
            self._t(
                "preset.load_error", path=gui_exports.condition_preset_path(),
                error=error))

    def _refresh_condition_preset_values(self, selected: str = "Custom"):
        if self._condition_preset_combo is not None:
            self._condition_preset_combo.configure(values=tuple(
                self._display_condition_preset(name)
                for name in self._condition_preset_names()))
        self._condition_preset_var.set(self._display_condition_preset(selected))
        self._update_preset_action_state()

    def _update_preset_action_state(self) -> None:
        """Expose whether the current conditions are saved without a modal."""
        selected = self._canonical_condition_preset(
            self._condition_preset_var.get())
        saved_custom = selected in getattr(self, "_custom_condition_presets", {})
        if getattr(self, "_delete_preset_btn", None) is not None:
            self._delete_preset_btn.configure(
                state="normal" if saved_custom else "disabled")
        if selected == "Custom" and hasattr(self, "_preset_status_var"):
            self._preset_status_var.set(self._t("preset.unsaved"))

    def _save_condition_preset(self):
        if self._preset_load_error is not None:
            self._show_preset_load_error()
            return
        try:
            conditions = self._read_conditions()
        except te.SequenceError as error:
            messagebox.showerror(self._t("preset.error_title"), str(error))
            return
        selected = self._canonical_condition_preset(
            self._condition_preset_var.get())
        initial = selected if selected in self._custom_condition_presets else ""
        name = simpledialog.askstring(
            self._t("preset.name_title"), self._t("preset.name_prompt"),
            initialvalue=initial, parent=self.master)
        if name is None:
            return
        try:
            normalized = gui_exports.normalize_preset_name(name)
            existing = next((
                item for item in self._custom_condition_presets
                if item.casefold() == normalized.casefold()), None)
            if existing is not None and not messagebox.askyesno(
                    self._t("preset.overwrite_title"),
                    self._t("preset.overwrite_body", name=existing)):
                return
            self._custom_condition_presets = gui_exports.upsert_condition_preset(
                normalized, conditions)
        except gui_exports.PersistenceError as error:
            messagebox.showerror(self._t("preset.error_title"), str(error))
            return
        self._refresh_condition_preset_values(normalized)
        self._update_condition_summary()
        self._preset_status_var.set(
            self._t("preset.saved_inline", name=normalized))

    def _delete_condition_preset(self):
        selected = self._canonical_condition_preset(
            self._condition_preset_var.get())
        if selected not in self._custom_condition_presets:
            self._preset_status_var.set(self._t("preset.custom_required"))
            return
        if not messagebox.askyesno(
                self._t("preset.delete_title"),
                self._t("preset.delete_body", name=selected)):
            return
        try:
            self._custom_condition_presets = gui_exports.delete_condition_preset(
                selected)
        except gui_exports.PersistenceError as error:
            messagebox.showerror(self._t("preset.error_title"), str(error))
            return
        self._refresh_condition_preset_values("Custom")
        self._update_condition_summary()
        self._preset_status_var.set(
            self._t("preset.deleted_inline", name=selected))

    def _reset_conditions(self):
        self._apply_condition_preset("qPCR / TaqMan")

    def _toggle_conditions(self):
        if self._condition_details is None:
            return
        self._conditions_expanded = not self._conditions_expanded
        if self._conditions_expanded:
            self._condition_details.grid()
            if self._condition_toggle_btn is not None:
                self._condition_toggle_btn.config(text=self._t("action.hide_details"))
        else:
            self._condition_details.grid_remove()
            if self._condition_toggle_btn is not None:
                self._condition_toggle_btn.config(text=self._t("action.show_details"))

    def _on_condition_preset_selected(self, _event=None):
        name = self._canonical_condition_preset(
            self._condition_preset_var.get())
        if name in self._all_condition_presets():
            self._apply_condition_preset(name)

    def _apply_condition_preset(self, name: str):
        d = self._all_condition_presets()[name]
        self._updating_conditions = True
        for key, var in self._cond_vars.items():
            var.set(f"{getattr(d, key):g}")
        self._updating_conditions = False
        self._condition_preset_var.set(self._display_condition_preset(name))
        self._preset_status_var.set("")
        self._update_preset_action_state()
        self._update_condition_summary()
        self._update_bulk_count()

    def _on_condition_change(self, *_):
        if self._updating_conditions:
            return
        self._condition_preset_var.set(self._display_condition_preset(
            self._matching_condition_preset() or "Custom"))
        self._update_preset_action_state()
        self._update_condition_summary()
        self._update_bulk_count()

    def _matching_condition_preset(self) -> str | None:
        try:
            cond = self._read_conditions()
        except te.SequenceError:
            return None
        for name, preset in self._all_condition_presets().items():
            if all(getattr(cond, key) == getattr(preset, key)
                   for _label, key in _COND_FIELDS):
                return name
        return None

    def _update_condition_summary(self):
        try:
            cond = self._read_conditions()
        except te.SequenceError:
            self._condition_summary_var.set(
                self._t("cond.invalid"))
            return
        preset = self._display_condition_preset(
            self._canonical_condition_preset(
                self._condition_preset_var.get()))
        self._condition_summary_var.set(
            self._t("cond.summary", preset=preset, mg=cond.dv_conc,
                    primer=cond.primer_conc, probe=cond.probe_conc,
                    dg_caution=cond.dg_caution,
                    dg_problem=cond.dg_problem,
                    near_difference=cond.near_duplicate_bond_difference,
                    gap_run=cond.dimer_max_consecutive_gaps,
                    gap_total=cond.dimer_max_total_gaps))

    def _read_conditions(self) -> te.ReactionConditions:
        vals = {}
        for key, var in self._cond_vars.items():
            raw = var.get().strip()
            try:
                vals[key] = parse_locale_number(
                    raw, integer=key in _INTEGER_CONDITION_FIELDS)
            except ValueError:
                raise te.SequenceError(
                    self._t("cond.must_be_number",
                            label=self._t(f"cond.label.{key}"), raw=raw))
        return te.ReactionConditions(**vals)

    # ------------------------------------------------------------------ #
    # Results widgets
    # ------------------------------------------------------------------ #
    def _set_summary(self, text: str, state: str = "idle"):
        bg, fg = _SUMMARY_STATES.get(state, _SUMMARY_STATES["idle"])
        self.summary.config(text="  " + text.lstrip(), bg=bg, fg=fg)

    def _build_results(self):
        container = ttk.Frame(self._workspace_pane, style="Workspace.TFrame")
        container.grid(row=0, column=0, sticky="nsew")
        container.columnconfigure(0, weight=1)
        container.rowconfigure(2, weight=1)

        status = ttk.Frame(container, style="Workspace.TFrame")
        status.grid(row=0, column=0, sticky="ew", pady=(0, _SPACE["xs"]))
        status.columnconfigure(0, weight=1)
        status.columnconfigure(1, weight=0)
        self.summary = tk.Label(status, text=self._t("status.enter_sequences"),
                                anchor="w", padx=_SPACE["sm"],
                                pady=_SPACE["xs"], relief="solid",
                                borderwidth=1, bg=_UI["summary_idle_bg"],
                                fg=_UI["text"], font=_FONT["body"])
        self.summary.grid(row=0, column=0, columnspan=4, sticky="ew")
        self.engine_health = ttk.Label(
            status, text=self._t("status.external_idle"), anchor="w",
            justify="left", style="TLabel")
        self.engine_health.grid(
            row=2, column=0, columnspan=4, sticky="ew",
            padx=_SPACE["sm"], pady=(_SPACE["xxs"], 0))

        utility = ttk.Frame(status, style="Workspace.TFrame")
        utility.grid(row=1, column=0, sticky="w", pady=(_SPACE["xxs"], 0))
        self._text(ttk.Label(utility, style="TLabel"),
                   "label.language").pack(side="left")
        self._language_combo = ttk.Combobox(
            utility, textvariable=self._language_var,
            values=tuple(_LANGUAGE_NAMES.values()), state="readonly",
            width=12, style="Lab.TCombobox")
        self._language_combo.pack(side="left", padx=(_SPACE["xs"], _SPACE["sm"]))
        self._language_combo.bind(
            "<<ComboboxSelected>>", self._set_language_from_var)
        self._text(ttk.Button(status, style="Secondary.TButton",
                              command=self._show_rules),
                   "action.rules").grid(row=1, column=2, sticky="e",
                                          pady=(_SPACE["xxs"], 0))
        self._bind_bounded_wrap(
            self.summary, minimum=260, inset=2 * _SPACE["sm"])
        self._bind_bounded_wrap(
            self.engine_health, minimum=260, inset=2 * _SPACE["sm"])

        self._build_overview(container).grid(
            row=1, column=0, sticky="ew", pady=(0, _SPACE["xs"]))

        nb = ttk.Notebook(container, style="Lab.TNotebook")
        self.results_nb = nb
        nb.grid(row=2, column=0, sticky="nsew")

        # --- Tm tab ---------------------------------------------------- #
        tm_tab = ttk.Frame(nb, padding=_SPACE["xs"], style="Lab.TFrame")
        nb.add(tm_tab)
        self._tab(nb, tm_tab, "tab.tm")
        tm_tab.columnconfigure(0, weight=1)
        tm_tab.rowconfigure(0, weight=1)
        cols = ("oligo", "len", "var", "gc", "conc", "tm", "tm_owcz", "seq")
        self.tm_tree = ttk.Treeview(
            tm_tab, columns=cols, show="headings", height=6,
            style="Lab.Treeview")
        for c, key, w, anc in [
            ("oligo", "heading.oligo", 115, "w"), ("len", "heading.len", 42, "e"),
            ("var", "heading.variants", 62, "e"),
            ("gc", "heading.gc", 70, "e"), ("conc", "heading.conc", 72, "e"),
            ("tm", "heading.tm_sl", 82, "e"), ("tm_owcz", "heading.tm_owcz", 82, "e"),
            ("seq", "heading.sequence", 300, "w"),
        ]:
            self._heading(self.tm_tree, c, key)
            self.tm_tree.column(c, width=w, anchor=anc, stretch=(c == "seq"))
        self.tm_tree.grid(row=0, column=0, sticky="nsew")
        tmsb = ttk.Scrollbar(
            tm_tab, orient="vertical", command=self.tm_tree.yview,
            style="Lab.Vertical.TScrollbar")
        tmsb.grid(row=0, column=1, sticky="ns")
        tmxsb = ttk.Scrollbar(
            tm_tab, orient="horizontal", command=self.tm_tree.xview,
            style="Lab.Horizontal.TScrollbar")
        tmxsb.grid(row=1, column=0, sticky="ew")
        self.tm_tree.configure(
            xscrollcommand=tmxsb.set, yscrollcommand=tmsb.set)

        # --- Flagged-structure triage tab ----------------------------- #
        problems_tab = ttk.Frame(nb, padding=_SPACE["xs"], style="Lab.TFrame")
        nb.add(problems_tab)
        self._tab(nb, problems_tab, "tab.flagged")
        self._problems_panel = self._make_problems_panel(problems_tab)

        # --- Hetero-dimer matrix scan view ---------------------------- #
        matrix_tab = ttk.Frame(nb, padding=_SPACE["xs"], style="Lab.TFrame")
        nb.add(matrix_tab)
        self._tab(nb, matrix_tab, "tab.matrix")
        self._matrix_panel = self._make_matrix_panel(matrix_tab)

        # --- Detailed structure tables, grouped under one top-level tab -- #
        structures_tab = ttk.Frame(nb, padding=_SPACE["xs"], style="Lab.TFrame")
        nb.add(structures_tab)
        self._tab(nb, structures_tab, "tab.structures")
        structures_tab.columnconfigure(0, weight=1)
        structures_tab.rowconfigure(0, weight=1)
        struct_nb = ttk.Notebook(structures_tab, style="Lab.TNotebook")
        struct_nb.grid(row=0, column=0, sticky="nsew")
        self._panels = {}
        for kind, title in [("Hairpin", "tab.hairpins"),
                            ("Self-dimer", "tab.self"),
                            ("Hetero-dimer", "tab.hetero")]:
            frame = ttk.Frame(struct_nb, padding=_SPACE["xs"],
                              style="Lab.TFrame")
            struct_nb.add(frame)
            self._tab(struct_nb, frame, title)
            self._panels[kind] = self._make_struct_panel(frame, kind)

        self._build_detail_inspector(container).grid(
            row=3, column=0, sticky="ew", pady=(_SPACE["sm"], 0))
        self._bind_result_tree_copy(self.tm_tree)
        self._bind_result_tree_copy(self._problems_panel["tree"])
        for panel in self._panels.values():
            self._bind_result_tree_copy(panel["tree"])
        self._bind_global_navigation()

    def _copy_result_tree_rows(self, tree: ttk.Treeview):
        """Copy selected displayed cells in visible row and column order."""
        selected = set(tree.selection())
        if not selected:
            return "break"
        source_columns = tuple(tree["columns"])
        raw_display = tree["displaycolumns"]
        display = ((raw_display,) if isinstance(raw_display, str)
                   else tuple(raw_display))
        columns = (source_columns if display == ("#all",) else display)
        indices = [source_columns.index(column) for column in columns]
        raw_show = tree["show"]
        show = (raw_show.split() if isinstance(raw_show, str)
                else tuple(raw_show))
        include_tree_column = "tree" in show
        lines = []

        def displayed_leaves(parent=""):
            for iid in tree.get_children(parent):
                children = tree.get_children(iid)
                if children:
                    yield from displayed_leaves(iid)
                else:
                    yield iid

        for iid in displayed_leaves():
            if iid not in selected:
                continue
            if "empty" in tree.item(iid, "tags"):
                continue
            values = tuple(tree.item(iid, "values"))
            if not values:
                continue
            cells = ([str(tree.item(iid, "text"))]
                     if include_tree_column else [])
            cells.extend(
                str(values[index]) if index < len(values) else ""
                for index in indices)
            lines.append("\t".join(cells))
        if not lines:
            return "break"
        self.master.clipboard_clear()
        self.master.clipboard_append("\n".join(lines))
        return "break"

    def _bind_result_tree_copy(self, tree: ttk.Treeview) -> None:
        tree.bind("<Control-c>",
                  lambda _event, widget=tree:
                  self._copy_result_tree_rows(widget), add="+")
        tree.bind("<Control-C>",
                  lambda _event, widget=tree:
                  self._copy_result_tree_rows(widget), add="+")
        menu = tk.Menu(tree, tearoff=0)
        self._configure_menu(menu)
        menu.add_command(
            label=self._t("action.copy_rows"),
            command=lambda widget=tree: self._copy_result_tree_rows(widget))
        self._localized_menus.append((menu, [(0, "action.copy_rows")]))

        def popup(event):
            row = tree.identify_row(event.y)
            if row and row not in tree.selection():
                tree.selection_set(row)
            menu.tk_popup(event.x_root, event.y_root)

        tree.bind("<Button-3>", popup, add="+")

    def _build_overview(self, parent) -> ttk.Frame:
        frame = ttk.Frame(parent, style="Lab.TFrame", padding=_SPACE["xs"])
        for idx in range(5):
            frame.columnconfigure(idx, weight=1)

        specs = [
            ("overview.verdict", "verdict", "overview.not_analyzed"),
            ("overview.problems", "problems", None),
            ("overview.cautions", "cautions", None),
            ("overview.prime_risks", "prime", None),
            ("overview.worst", "worst", None),
        ]
        for idx, (title, key, default) in enumerate(specs):
            cell = ttk.Frame(frame, padding=(_SPACE["xs"], _SPACE["xxs"]),
                             style="Lab.TFrame")
            cell.grid(row=0, column=idx, sticky="ew")
            self._text(ttk.Label(cell, style="MetricTitle.TLabel"),
                       title).pack(anchor="w")
            var = tk.StringVar(value=self._t(default) if default else ("-" if key == "worst" else "0"))
            self._overview_vars[key] = var
            ttk.Label(cell, textvariable=var,
                      style="MetricValue.TLabel").pack(anchor="w")

        top_risks = ttk.Label(
            frame, textvariable=self._top_risks_var,
            style="Muted.TLabel", justify="left", wraplength=720)
        top_risks.grid(
            row=1, column=0, columnspan=5, sticky="ew",
            pady=(_SPACE["xxs"], 0))
        frame.bind(
            "<Configure>",
            lambda e, label=top_risks: label.configure(
                wraplength=max(260, e.width - _SPACE["md"])))
        return frame

    def _build_detail_inspector(self, parent) -> ttk.LabelFrame:
        frame = ttk.LabelFrame(parent, padding=_SPACE["sm"],
                               style="Section.TLabelframe")
        self._text(frame, "detail.selected")
        self._detail_frame = frame
        frame.columnconfigure(0, weight=1)

        head = ttk.Frame(frame, style="Lab.TFrame")
        head.grid(row=0, column=0, sticky="ew", pady=(0, _SPACE["xs"]))
        head.columnconfigure(0, weight=1)
        ttk.Label(
            head, textvariable=self._detail_title_var,
            style="MetricValue.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            head, textvariable=self._detail_status_var,
            style="Muted.TLabel").grid(row=1, column=0, sticky="w")
        self._detail_diagram_btn = ttk.Button(
            head, command=self._show_selected_diagram,
            state="disabled", style="Secondary.TButton")
        self._text(self._detail_diagram_btn, "action.show_diagram")
        self._detail_diagram_btn.grid(row=0, column=1, rowspan=2, sticky="e")

        text = tk.Text(
            frame, height=3, font=_FONT["mono"], wrap="none", takefocus=1,
            bg=_UI["detail_bg"], fg=_UI["text"],
            insertbackground=_UI["text"], selectbackground=_UI["accent_soft"],
            relief="solid", borderwidth=1, highlightthickness=1,
            highlightbackground=_UI["border"], highlightcolor=_UI["focus"],
            padx=_SPACE["xs"], pady=_SPACE["xs"])
        text.grid(row=1, column=0, sticky="nsew")
        ysb = ttk.Scrollbar(
            frame, orient="vertical", command=text.yview,
            style="Lab.Vertical.TScrollbar")
        ysb.grid(row=1, column=1, sticky="ns")
        xsb = ttk.Scrollbar(
            frame, orient="horizontal", command=text.xview,
            style="Lab.Horizontal.TScrollbar")
        xsb.grid(row=2, column=0, sticky="ew")
        text.configure(
            xscrollcommand=xsb.set, yscrollcommand=ysb.set,
            state="disabled")
        self._attach_text_shortcuts(text)
        # Read-only details remain keyboard-selectable/copyable. Text's class
        # binding consumes Shift-Tab, so restore reverse traversal locally.
        def previous_control(_event):
            text.tk_focusPrev().focus_set()
            return "break"
        text.bind("<Shift-Tab>", previous_control)
        self._detail_text = text
        return frame

    def _show_help_topic(self, topic: str, opener=None):
        if topic not in HELP_TOPICS:
            return
        focus_return = opener or self.master.focus_get()
        win = tk.Toplevel(self.master)
        win.title(self._t(f"help.{topic}"))
        win.geometry("560x300")
        win.minsize(420, 220)
        win.transient(self.master)
        apply_window_icon(win)
        frame = ttk.Frame(win, padding=_SPACE["md"], style="Lab.TFrame")
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        text = tk.Text(
            frame, wrap="word", takefocus=1, bg=_UI["surface"],
            fg=_UI["text"], insertbackground=_UI["text"],
            selectbackground=_UI["accent_soft"], relief="solid",
            borderwidth=1, highlightthickness=1,
            highlightbackground=_UI["border"], highlightcolor=_UI["focus"],
            padx=_SPACE["sm"], pady=_SPACE["sm"], font=_FONT["body"])
        text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(
            frame, orient="vertical", command=text.yview,
            style="Lab.Vertical.TScrollbar")
        scrollbar.grid(row=0, column=1, sticky="ns")
        text.configure(yscrollcommand=scrollbar.set)
        text.insert("1.0", HELP_TOPICS[topic][self._language_code])
        text.configure(state="disabled")
        self._attach_text_shortcuts(text)

        def close(_event=None):
            win.destroy()
            if focus_return is not None:
                try:
                    focus_return.focus_set()
                except tk.TclError:
                    pass

        close_button = ttk.Button(
            frame, text=self._t("action.close"), command=close,
            style="Secondary.TButton")
        close_button.grid(row=1, column=0, columnspan=2, sticky="e",
                          pady=(_SPACE["sm"], 0))
        win.bind("<Escape>", close)
        win.protocol("WM_DELETE_WINDOW", close)
        text.focus_set()

    def _show_rules(self):
        try:
            cond = self._read_conditions()
        except te.SequenceError:
            cond = te.ReactionConditions()
        if vb.available():
            vienna_text = (
                self._t("dialog.vienna_available", version=vb.version()))
        else:
            vienna_text = (
                self._t("dialog.vienna_missing"))
        body = self._t(
            "dialog.rules_body", dg_caution=cond.dg_caution,
            dg_problem=cond.dg_problem,
            engine_method_text=self._t("dialog.engine_methods"),
            vienna_text=vienna_text,
            bundled_engine_text=self._t("dialog.bundled_engines"))
        if not hasattr(self, "master"):
            _show_rules_compat_message(self._t("dialog.rules_title"), body)
            return
        focus_return = self.master.focus_get()
        win = tk.Toplevel(self.master)
        win.title(self._t("dialog.rules_title"))
        win.geometry("760x620")
        win.minsize(560, 400)
        win.transient(self.master)
        apply_window_icon(win)
        frame = ttk.Frame(win, padding=_SPACE["md"], style="Lab.TFrame")
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        ttk.Label(
            frame, text=self._t("dialog.rules_title"),
            style="MetricValue.TLabel").grid(row=0, column=0, sticky="w",
                                              pady=(0, _SPACE["sm"]))
        text = tk.Text(
            frame, wrap="word", takefocus=1, bg=_UI["surface"],
            fg=_UI["text"], insertbackground=_UI["text"],
            selectbackground=_UI["accent_soft"], relief="solid",
            borderwidth=1, highlightthickness=1,
            highlightbackground=_UI["border"], highlightcolor=_UI["focus"],
            padx=_SPACE["sm"], pady=_SPACE["sm"], font=_FONT["body"])
        text.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(
            frame, orient="vertical", command=text.yview,
            style="Lab.Vertical.TScrollbar")
        scrollbar.grid(row=1, column=1, sticky="ns")
        text.configure(yscrollcommand=scrollbar.set)
        text.insert("end", body)
        text.configure(state="disabled")
        self._attach_text_shortcuts(text)

        def close(_event=None):
            win.destroy()
            if focus_return is not None:
                try:
                    focus_return.focus_set()
                except tk.TclError:
                    pass

        close_button = ttk.Button(
            frame, text=self._t("action.close"), command=close,
            style="Secondary.TButton")
        close_button.grid(row=2, column=0, columnspan=2, sticky="e",
                          pady=(_SPACE["sm"], 0))
        win.bind("<Escape>", close)
        win.protocol("WM_DELETE_WINDOW", close)
        text.focus_set()

    def _make_problems_panel(self, frame) -> dict:
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        filters = ttk.Frame(frame, style="Lab.TFrame")
        filters.grid(row=0, column=0, columnspan=2, sticky="ew",
                     pady=(0, _SPACE["xs"]))
        for col in (1, 3):
            filters.columnconfigure(col, weight=1)
        filter_specs = [
            ("severity", "filter.severity", 0, 0, 16),
            ("kind", "filter.type", 0, 2, 18),
        ]
        for key, label, row, col, width in filter_specs:
            self._text(ttk.Label(filters, style="Lab.TLabel"), label).grid(
                row=row, column=col, sticky="w",
                padx=(0 if col == 0 else _SPACE["sm"], _SPACE["xs"]))
            var = tk.StringVar(value=self._display_option(_FILTER_OPTIONS[key][0]))
            self._filter_vars[key] = var
            combo = ttk.Combobox(
                filters, textvariable=var,
                values=tuple(self._display_option(v) for v in _FILTER_OPTIONS[key]),
                state="readonly", width=width,
                style="Lab.TCombobox")
            self._localized_filter_combos[key] = combo
            combo.grid(row=row, column=col + 1, sticky="ew")
            combo.bind("<<ComboboxSelected>>", lambda _e: self._refresh_problems_first())

        self._text(ttk.Label(filters, style="Lab.TLabel"), "filter.search").grid(
            row=1, column=0, sticky="w", pady=(_SPACE["xs"], 0))
        search = tk.StringVar()
        self._filter_vars["search"] = search
        search_entry = ttk.Entry(filters, textvariable=search, width=24,
                                 style="Lab.TEntry")
        search_entry.grid(row=1, column=1, columnspan=2, sticky="ew",
                          pady=(_SPACE["xs"], 0))
        self._attach_text_shortcuts(search_entry)
        search.trace_add(
            "write", lambda *_: self._refresh_problems_on_filter_change())
        prime_check = ttk.Checkbutton(
            filters, variable=self._prime_risks_only_var,
            command=self._refresh_problems_first,
            style="Lab.TCheckbutton")
        self._text(prime_check, "filter.prime_only").grid(
            row=1, column=3, columnspan=2, sticky="w",
            padx=(_SPACE["sm"], 0), pady=(_SPACE["xs"], 0))
        self._text(ttk.Button(filters, style="Secondary.TButton",
                              command=self._clear_problem_filters),
                   "action.clear_filters").grid(
            row=1, column=5, sticky="e", padx=(_SPACE["sm"], 0),
            pady=(_SPACE["xs"], 0))
        count = ttk.Label(filters, text=self._t("filter.showing_zero"),
                          style="Muted.TLabel")
        count.grid(row=2, column=0, columnspan=3, sticky="w",
                   pady=(_SPACE["xs"], 0))
        active = ttk.Label(filters, text=self._t("filter.active_default"),
                           style="Muted.TLabel", justify="right",
                           wraplength=520)
        active.grid(row=2, column=3, columnspan=3, sticky="e",
                    pady=(_SPACE["xs"], 0))
        sort_status = ttk.Label(
            filters, text=self._t("sort.default"), style="Muted.TLabel")
        sort_status.grid(row=3, column=0, columnspan=6, sticky="w",
                         pady=(_SPACE["xxs"], 0))
        filters.bind(
            "<Configure>",
            lambda e, label=active: label.configure(
                wraplength=max(300, e.width // 2)))

        cols = self._problem_result_columns()
        tree = ttk.Treeview(
            frame, columns=cols, displaycolumns=_FLAGGED_DISPLAY_COLUMNS,
            show="headings", height=9, style="Lab.Treeview")
        heading_keys = {}
        for c, key, w, anc in [
            ("severity", "heading.severity", 66, "w"),
            ("kind", "heading.kind", 82, "w"),
            ("site", "heading.flag", 58, "w"),
            ("label", "heading.label", 150, "w"),
            ("n", "heading.n", 42, "e"),
            ("dg", "heading.dg", 66, "e"),
            ("tm", "heading.tm", 66, "e"),
            ("rank", "heading.rank", 54, "e"),
            ("discovered", "heading.discovered_by", 120, "w"),
            ("assessment", "heading.assessment", 260, "w"),
        ]:
            self._heading(
                tree, c, key,
                command=lambda col=c: self._sort_problem_column(col))
            heading_keys[c] = key
            tree.column(c, width=w, anchor=anc, stretch=(c == "assessment"))
        tree.grid(row=1, column=0, sticky="nsew")
        sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview,
                           style="Lab.Vertical.TScrollbar")
        sb.grid(row=1, column=1, sticky="ns")
        xsb = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview,
                            style="Lab.Horizontal.TScrollbar")
        xsb.grid(row=2, column=0, sticky="ew")
        tree.configure(xscrollcommand=xsb.set, yscrollcommand=sb.set)
        flagged_backgrounds = {
            "ok": _COLOR["surface"],
            "caution": _COLOR["caution_bg"],
            "problem": _COLOR["problem_bg"],
            "unclassified": _COLOR["surface"],
        }
        for sev, colour in flagged_backgrounds.items():
            tree.tag_configure(
                sev, background=colour, foreground=_UI["text"])
        tree.tag_configure("empty", foreground=_UI["muted"])

        panel = {
            "tree": tree, "iid": {}, "count": count, "active": active,
            "sort": sort_status, "heading_keys": heading_keys,
        }
        tree.bind("<<TreeviewSelect>>", lambda e, p=panel: self._panel_show(p))
        tree.bind("<Double-1>", lambda e, p=panel: self._panel_diagram(p))
        return panel

    def _make_matrix_panel(self, frame) -> dict:
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        tools = ttk.Frame(frame, style="Lab.TFrame")
        tools.grid(row=0, column=0, columnspan=2, sticky="ew",
                   pady=(0, _SPACE["xs"]))
        self._text(ttk.Checkbutton(
            tools,
            variable=self._matrix_flagged_only_var,
            command=self._refresh_matrix,
            style="Lab.TCheckbutton"), "matrix.show_flagged").pack(side="left")
        legend = ttk.Frame(tools, style="Lab.TFrame")
        legend.pack(side="left", padx=(_SPACE["md"], 0))
        legend_labels = []
        for key in (
            "matrix.legend.problem",
            "matrix.legend.caution",
            "matrix.legend.prime",
            "matrix.legend.self",
        ):
            label = self._text(ttk.Label(legend, style="Muted.TLabel"), key)
            label.pack(side="left", padx=(0, _SPACE["sm"]))
            legend_labels.append(label)
        status = ttk.Label(tools, text=self._t("matrix.status_click"),
                           style="Muted.TLabel")
        status.pack(side="right")

        canvas = tk.Canvas(
            frame, height=260, bg=_UI["surface"], highlightthickness=1,
            highlightbackground=_UI["border"], highlightcolor=_UI["focus"],
            borderwidth=0, cursor="hand2", takefocus=1)
        canvas.grid(row=1, column=0, sticky="nsew")
        ysb = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview,
                            style="Lab.Vertical.TScrollbar")
        ysb.grid(row=1, column=1, sticky="ns")
        xsb = ttk.Scrollbar(frame, orient="horizontal", command=canvas.xview,
                            style="Lab.Horizontal.TScrollbar")
        xsb.grid(row=2, column=0, sticky="ew")
        canvas.configure(xscrollcommand=xsb.set, yscrollcommand=ysb.set)
        canvas.bind("<Button-1>", self._matrix_click)
        canvas.bind("<Double-1>", lambda _e: self._matrix_diagram())
        canvas.bind("<Return>", lambda _e: self._matrix_diagram())
        canvas.bind("<Left>", lambda _e: self._move_matrix_selection(0, -1))
        canvas.bind("<Right>", lambda _e: self._move_matrix_selection(0, 1))
        canvas.bind("<Up>", lambda _e: self._move_matrix_selection(-1, 0))
        canvas.bind("<Down>", lambda _e: self._move_matrix_selection(1, 0))

        return {"canvas": canvas, "status": status, "legend": legend_labels}

    def _make_struct_panel(self, frame, kind: str) -> dict:
        """Build one structure table for a single structure type."""
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        scols = (
            "severity", "site", "n", "dg", "tm", "rank",
            "discovered", "assessment",
        )
        tree = ttk.Treeview(
            frame, columns=scols, displaycolumns=_STRUCTURE_DISPLAY_COLUMNS,
            show="tree headings", height=9, style="Lab.Treeview")
        self._heading(tree, "#0", "heading.structure")
        tree.column("#0", width=250, anchor="w", stretch=False)
        for c, key, w, anc in [
            ("severity", "heading.severity", 66, "w"),
            ("site", "heading.flag", 58, "w"),
            ("n", "heading.n", 42, "e"),
            ("dg", "heading.dg", 66, "e"),
            ("tm", "heading.tm", 66, "e"),
            ("rank", "heading.rank", 54, "e"),
            ("discovered", "heading.discovered_by", 120, "w"),
            ("assessment", "heading.assessment", 260, "w"),
        ]:
            self._heading(tree, c, key)
            tree.column(c, width=w, anchor=anc, stretch=(c == "assessment"))
        tree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview,
                           style="Lab.Vertical.TScrollbar")
        sb.grid(row=0, column=1, sticky="ns")
        xsb = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview,
                            style="Lab.Horizontal.TScrollbar")
        xsb.grid(row=1, column=0, sticky="ew")
        tree.configure(yscrollcommand=sb.set, xscrollcommand=xsb.set)
        for sev, colour in _SEV_COLOR.items():
            tree.tag_configure(sev, background=colour, foreground=_SEV_FG[sev])

        panel = {
            "tree": tree, "iid": {}, "kind": kind,
            "aggregate_columns": True,
        }
        tree.bind("<<TreeviewSelect>>", lambda e, p=panel: self._panel_show(p))
        tree.bind("<Double-1>", lambda e, p=panel: self._panel_diagram(p))
        return panel

    # ------------------------------------------------------------------ #
    # Action buttons
    # ------------------------------------------------------------------ #
    def _additional_analysis_summary(self) -> str:
        key = ("ensemble.summary_on" if self._ensemble_complete_var.get()
               else "ensemble.summary_off")
        size = self._ensemble_budget_var.get().strip() or "0"
        return self._t(key, size=size)

    def _update_additional_analysis_summary(self, *_args) -> None:
        if hasattr(self, "_additional_analysis_summary_var"):
            self._additional_analysis_summary_var.set(
                self._additional_analysis_summary())

    def _toggle_additional_analysis(self) -> None:
        details = self._additional_analysis_details
        if details is None:
            return
        self._additional_analysis_expanded = not self._additional_analysis_expanded
        if self._additional_analysis_expanded:
            details.grid()
        else:
            details.grid_remove()
        if self._additional_analysis_toggle_btn is not None:
            self._additional_analysis_toggle_btn.configure(
                text=self._t("action.collapse" if self._additional_analysis_expanded
                             else "action.expand"))

    def _build_actions(self):
        parent = self._setup_pane
        bar = ttk.LabelFrame(parent, padding=_SPACE["sm"],
                             style="Section.TLabelframe")
        self._text(bar, "run.title")
        bar.grid(row=2, column=0, sticky="ew")
        bar.columnconfigure(0, weight=1)
        bar.columnconfigure(1, weight=1)

        ttk.Label(
            bar, textvariable=self._additional_analysis_summary_var,
            style="Muted.TLabel", wraplength=230, justify="left").grid(
                row=0, column=0, sticky="w")
        self._additional_analysis_toggle_btn = ttk.Button(
            bar, command=self._toggle_additional_analysis,
            style="Secondary.TButton")
        self._additional_analysis_toggle_btn.configure(
            text=self._t("action.collapse" if self._additional_analysis_expanded
                         else "action.expand"))
        self._additional_analysis_toggle_btn.grid(
            row=0, column=1, sticky="e", padx=(_SPACE["sm"], 0))

        details = ttk.Frame(bar, style="Lab.TFrame")
        details.grid(row=1, column=0, columnspan=2, sticky="ew",
                     pady=(_SPACE["xs"], 0))
        details.columnconfigure(0, weight=1)
        self._additional_analysis_details = details
        mode_check = ttk.Checkbutton(
            details, variable=self._ensemble_complete_var,
            style="Lab.TCheckbutton")
        self._text(mode_check, "ensemble.complete_mode")
        mode_check.grid(row=0, column=0, columnspan=3, sticky="w")
        self._text(ttk.Label(details, style="Lab.TLabel"),
                   "ensemble.additional_budget").grid(
                       row=1, column=0, sticky="w",
                       pady=(_SPACE["xs"], 0))
        budget_entry = ttk.Entry(
            details, textvariable=self._ensemble_budget_var, width=8,
            style="Lab.TEntry")
        budget_entry.grid(row=1, column=1, sticky="e",
                          pady=(_SPACE["xs"], 0))
        self._attach_text_shortcuts(budget_entry)
        additional_help = ttk.Button(
            details, text="?", width=2, takefocus=True,
            style="Secondary.TButton")
        additional_help.configure(
            command=lambda b=additional_help:
            self._show_help_topic("additional_analysis", b))
        additional_help.grid(row=1, column=2, sticky="e",
                             padx=(_SPACE["xs"], 0),
                             pady=(_SPACE["xs"], 0))
        if not self._additional_analysis_expanded:
            details.grid_remove()
        self._ensemble_complete_var.trace_add(
            "write", self._update_additional_analysis_summary)
        self._ensemble_budget_var.trace_add(
            "write", self._update_additional_analysis_summary)
        self._update_additional_analysis_summary()

        self.analyze_btn = ttk.Button(
            bar, style="Accent.TButton", command=self.analyze)
        self._text(self.analyze_btn, "action.analyze")
        self.analyze_btn.grid(row=2, column=0, columnspan=2, sticky="ew",
                              pady=(_SPACE["sm"], 0))
        self.cancel_btn = ttk.Button(
            bar, command=self.cancel_analysis, state="disabled",
            style="Danger.TButton")
        self._text(self.cancel_btn, "action.cancel")
        self.cancel_btn.grid(row=3, column=0, sticky="ew", pady=(_SPACE["sm"], 0))
        self.clear_btn = ttk.Button(bar, command=self._clear,
                                    style="Secondary.TButton")
        self._text(self.clear_btn, "action.clear")
        self.clear_btn.grid(row=4, column=0, sticky="ew",
                            pady=(_SPACE["sm"], 0))
        self.progress_value = tk.DoubleVar(value=0)
        self.progress = ttk.Progressbar(
            bar, length=180, mode="determinate", maximum=1,
            variable=self.progress_value,
            style="Status.Horizontal.TProgressbar")
        self.progress.grid(row=5, column=0, columnspan=2, sticky="ew",
                           pady=(_SPACE["sm"], 0))
        self.progress_label = ttk.Label(bar, text=self._t("progress.idle"), anchor="w",
                                        justify="left", style="Status.TLabel")
        self.progress_label.grid(row=6, column=0, columnspan=2, sticky="ew",
                                 pady=(_SPACE["xs"], 0))
        self._bind_bounded_wrap(
            self.progress_label, minimum=180, inset=2 * _SPACE["sm"])
        self.export_btn = ttk.Menubutton(
            bar, state="disabled",
            style="Secondary.TMenubutton")
        self._text(self.export_btn, "action.export")
        export_menu = tk.Menu(self.export_btn, tearoff=0)
        self._configure_menu(export_menu)
        entries = []
        export_menu.add_command(
            label=self._t("action.text_report"), command=lambda: self._export("text"))
        entries.append((0, "action.text_report"))
        export_menu.add_command(
            label=self._t("action.csv_report"), command=lambda: self._export("csv"))
        entries.append((1, "action.csv_report"))
        export_menu.add_command(
            label=self._t("action.tsv_report"), command=lambda: self._export("tsv"))
        entries.append((2, "action.tsv_report"))
        export_menu.add_separator()
        export_menu.add_command(
            label=self._t("action.csv_flagged"),
            command=lambda: self._export("flagged_csv"))
        entries.append((4, "action.csv_flagged"))
        export_menu.add_command(
            label=self._t("action.csv_matrix"),
            command=lambda: self._export("matrix_csv"))
        entries.append((5, "action.csv_matrix"))
        export_menu.add_separator()
        export_menu.add_command(
            label=self._t("action.run_archive"),
            command=lambda: self._export("run_archive"))
        entries.append((7, "action.run_archive"))
        self._localized_menus.append((export_menu, entries))
        self.export_btn["menu"] = export_menu
        self.export_btn.grid(row=3, column=1, sticky="ew",
                             padx=(_SPACE["sm"], 0), pady=(_SPACE["sm"], 0))
        self.import_run_btn = ttk.Button(
            bar, command=self._import_analyzed_run,
            style="Secondary.TButton")
        self._text(self.import_run_btn, "action.import_run")
        self.import_run_btn.grid(
            row=4, column=1, sticky="ew", padx=(_SPACE["sm"], 0),
            pady=(_SPACE["sm"], 0))

    # ------------------------------------------------------------------ #
    # Behaviour
    # ------------------------------------------------------------------ #
    def analyze(self):
        if self._analysis_running():
            return

        try:
            cond = self._read_conditions()
            if self._using_bulk():
                items = te.parse_bulk_oligos(self.bulk_text.get("1.0", "end"))
                if len(items) < 1:
                    raise te.SequenceError(
                        self._t("validation.bulk_empty"))
                oligos = te.build_oligo_list(items, cond)
            else:
                oligos = te.build_oligos(
                    self._seq_vars["fwd"].get(),
                    self._seq_vars["rev"].get(),
                    self._seq_vars["probe1"].get(),
                    self._seq_vars["probe2"].get(),
                    cond,
                )
            try:
                ensemble_budget = int(self._ensemble_budget_var.get().strip())
            except ValueError as exc:
                raise te.SequenceError(
                    self._t("ensemble.invalid_budget")) from exc
            if ensemble_budget < 0:
                raise te.SequenceError(self._t("ensemble.invalid_budget"))
        except te.SequenceError as e:
            messagebox.showerror(self._t("dialog.input_error"), str(e))
            return

        ensemble_mode = (
            te.ENSEMBLE_MODE_BUDGETED_COMPLETE
            if self._ensemble_complete_var.get()
            else te.ENSEMBLE_MODE_REPRESENTATIVE)
        self._start_analysis_worker(
            oligos, cond, ensemble_mode, ensemble_budget)

    def cancel_analysis(self):
        if getattr(self, "_import_thread", None) is not None:
            self._import_cancel.set()
            self.cancel_btn.config(state="disabled")
            self.progress_label.config(text=self._t("progress.cancelling"))
            return
        pending = getattr(self, "_analysis_pending", None)
        if pending is None:
            pending = self._analysis_running()
        if (not pending or self._cancel_event is None):
            return
        self._cancel_event.set()
        self._progress_cancel_requested = True
        self._stop_progress_pulse()
        self.cancel_btn.config(state="disabled")
        self.progress_label.config(text=self._t("progress.cancelling"))
        self._set_summary(self._t("progress.cancelling"), "info")

    def _analysis_running(self) -> bool:
        return bool((self._analysis_thread and self._analysis_thread.is_alive())
                    or getattr(self, "_import_thread", None) is not None)

    def _request_close(self):
        """Keep servicing Tk until the owned worker acknowledges cancellation."""
        self._closing = True
        self._populate_generation += 1
        self._cancel_render_jobs()
        if self._analysis_running():
            self.cancel_analysis()
            self.after(25, self._request_close)
        else:
            self.master.destroy()

    def _start_analysis_worker(
        self, oligos, cond: te.ReactionConditions,
        ensemble_mode: str = te.ENSEMBLE_MODE_REPRESENTATIVE,
        ensemble_budget: int = te.DEFAULT_ENSEMBLE_ADDITIONAL_BUDGET,
    ):
        work_plan = te.plan_ensemble_work(
            oligos, mode=ensemble_mode, additional_budget=ensemble_budget)
        self._analysis_queue = queue.Queue()
        self._cancel_event = threading.Event()
        self._analysis_pending = True
        # Keep the previous successful report visible until a new one finishes.
        self._set_busy(True)
        self._progress_has_completed_work = False
        self._progress_cancel_requested = False
        self._last_analysis_progress = None
        self.progress_value.set(0)
        self.progress.configure(
            maximum=max(1, len(oligos) + work_plan.allocated_contexts))
        self._start_progress_pulse()
        self._progress_start_counts = (
            work_plan.allocated_contexts, work_plan.total_contexts)
        self.progress_label.config(text=self._t(
            "progress.starting_estimate",
            allocated=work_plan.allocated_contexts,
            total=work_plan.total_contexts))
        self._set_summary(
            self._t("status.analysis_running_previous" if self._last_report
                    else "status.analysis_running_empty"),
            "info",
        )

        self._analysis_thread = threading.Thread(
            target=self._analysis_worker,
            args=(oligos, cond, self._cancel_event, self._analysis_queue,
                  ensemble_mode, ensemble_budget),
            daemon=True,
        )
        self._analysis_thread.start()
        self._poll_analysis_queue()

    @staticmethod
    def _analysis_worker(oligos, cond: te.ReactionConditions,
                         cancel_event: threading.Event,
                         result_queue: queue.Queue,
                         ensemble_mode: str = te.ENSEMBLE_MODE_REPRESENTATIVE,
                         ensemble_budget: int = (
                             te.DEFAULT_ENSEMBLE_ADDITIONAL_BUDGET)) -> None:
        last_progress_at = 0.0

        def on_progress(progress: te.AnalysisProgress) -> None:
            nonlocal last_progress_at
            now = time.monotonic()
            if (progress.completed in {0, progress.total}
                    or now - last_progress_at >= 0.1):
                result_queue.put(("progress", progress))
                last_progress_at = now

        try:
            report = te.analyze(
                oligos,
                cond,
                progress_callback=on_progress,
                cancel_event=cancel_event,
                ensemble_mode=ensemble_mode,
                ensemble_additional_budget=ensemble_budget,
            )
        except te.AnalysisCancelled:
            result_queue.put(("cancelled", None))
        except Exception as e:  # pragma: no cover - defensive
            result_queue.put(("error", (type(e).__name__, str(e))))
        else:
            if cancel_event.is_set():
                result_queue.put(("cancelled", None))
            else:
                result_queue.put(("done", (oligos, cond, report)))

    def _poll_analysis_queue(self):
        result_queue = self._analysis_queue
        if result_queue is None:
            return

        if self._drain_analysis_messages(result_queue):
            return
        if self._analysis_running():
            self.after(100, self._poll_analysis_queue)
            return
        if self._drain_final_analysis_message(result_queue):
            return
        self._finish_analysis_error(
            "RuntimeError", "Analysis worker stopped without a result.")

    def _drain_analysis_messages(self, result_queue: queue.Queue) -> bool:
        while True:
            try:
                message = result_queue.get_nowait()
            except queue.Empty:
                return False
            if self._handle_analysis_message(message):
                return True

    def _drain_final_analysis_message(self, result_queue: queue.Queue) -> bool:
        try:
            message = result_queue.get_nowait()
        except queue.Empty:
            return False
        return self._handle_analysis_message(message)

    def _handle_analysis_message(self, message) -> bool:
        kind, payload = message
        if kind == "progress":
            self._show_progress(payload)
            return False
        if kind == "partial":
            # Compatibility guard for stale workers: core-only reports may
            # update status, but never replace the last complete result set.
            if hasattr(self, "summary"):
                self._set_summary(self._t("status.core_complete"), "info")
            if hasattr(self, "progress_label"):
                self.progress_label.config(text=self._t("status.core_complete"))
            return False
        if kind == "done":
            cancel_event = getattr(self, "_cancel_event", None)
            if cancel_event is not None and cancel_event.is_set():
                self._finish_analysis_cancelled()
                return True
            oligos, cond, report = payload
            if (getattr(report, "complete", True) is not True
                    or getattr(report, "phase", "complete") != "complete"):
                self._finish_analysis_error(
                    "AnalysisIncompleteError",
                    f"analysis ended in non-final phase '{report.phase}'",
                )
                return True
            self._finish_analysis_success(oligos, cond, report)
            return True
        if kind == "cancelled":
            self._finish_analysis_cancelled()
            return True
        if kind == "error":
            err_type, err_text = payload
            self._finish_analysis_error(err_type, err_text)
            return True
        return False

    def _start_progress_pulse(self):
        if getattr(self, "_progress_indeterminate", False):
            return
        self.progress.configure(mode="indeterminate")
        self.progress.start()
        self._progress_indeterminate = True

    def _stop_progress_pulse(self):
        if getattr(self, "_progress_indeterminate", False):
            self.progress.stop()
        self.progress.configure(mode="determinate")
        self._progress_indeterminate = False

    def _show_progress(self, progress: te.AnalysisProgress):
        self._last_analysis_progress = progress
        maximum = progress.total or 1
        self.progress.configure(maximum=maximum)
        self.progress_value.set(progress.completed)
        if progress.completed > 0:
            self._progress_has_completed_work = True
            self._stop_progress_pulse()
        elif (not getattr(self, "_progress_has_completed_work", False)
              and not getattr(self, "_progress_cancel_requested", False)):
            self._start_progress_pulse()
        message = self._display_progress_message(progress.message)
        if progress.total:
            self.progress_label.config(
                text=f"{progress.completed}/{progress.total} - {message}")
        else:
            self.progress_label.config(text=message)

    def _finish_analysis_success(self, oligos, cond, report: te.AnalysisReport):
        self._analysis_pending = False
        self._stop_progress_pulse()
        if not report.complete or report.phase != "complete":
            self._finish_analysis_error(
                "AnalysisIncompleteError",
                f"analysis ended in non-final phase '{report.phase}'",
            )
            return
        total_steps = (len(oligos) + report.ensemble_plan.allocated_contexts
                       if report.ensemble_plan is not None
                       else te.analysis_step_count(oligos))
        self.progress.configure(maximum=max(1, total_steps))
        self.progress_value.set(total_steps)
        self.progress_label.config(text=self._t("progress.complete"))

        previous = (
            getattr(self, "_last_oligos", None),
            getattr(self, "_last_report", None),
        )
        previous_detail = getattr(self, "_detail_structure", None)
        self._render_failure_kind = "analysis"
        self._render_on_complete = None
        self._render_recovery = {
            "oligos": previous[0], "report": previous[1],
            "condition": getattr(self, "_last_cond", None),
            "detail": previous_detail,
        }
        try:
            self._populate(oligos, report)
        except Exception as error:
            self._render_recovery = None
            self._render_failure_kind = None
            diagnostics = str(error)
            try:
                if previous[0] is not None and previous[1] is not None:
                    self._populate(previous[0], previous[1])
                    if previous_detail is not None:
                        self._show_structure_detail(previous_detail)
                else:
                    self._clear_result_tables()
                    self._reset_overview()
            except Exception as restore_error:
                diagnostics += (
                    "\nUI restoration failed: "
                    f"{type(restore_error).__name__}: {restore_error}")
            self._finish_analysis_error(type(error).__name__, diagnostics)
            return
        self._last_report, self._last_oligos, self._last_cond = (
            report, oligos, cond)
        self._set_busy(False)
        self._finish_render_jobs()

    def _finish_analysis_cancelled(self):
        self._analysis_pending = False
        self._stop_progress_pulse()
        self._set_busy(False)
        self.progress_label.config(text=self._t("progress.cancelled"))
        tail = self._t("status.cancelled_tail") if self._last_report else ""
        self._set_summary(self._t("status.analysis_cancelled", tail=tail), "idle")

    def _analysis_failure_message(self, err_type: str, err_text: str) -> str:
        category = _analysis_failure_category(err_type)
        return "\n\n".join((
            self._t(f"dialog.failure_summary.{category}"),
            self._t(f"dialog.failure_recovery.{category}"),
            self._t(
                "dialog.failure_technical", error_type=err_type,
                error=err_text),
        ))

    def _finish_analysis_error(self, err_type: str, err_text: str):
        self._analysis_pending = False
        self._stop_progress_pulse()
        self._set_busy(False)
        self.progress_label.config(text=self._t("progress.failed"))
        tail = self._t("status.cancelled_tail") if self._last_report else ""
        self._set_summary(self._t("status.analysis_failed", tail=tail), "problem")
        messagebox.showerror(
            self._t("dialog.calc_error"),
            self._analysis_failure_message(err_type, err_text))

    def _set_busy(self, busy: bool):
        self.master.config(cursor="watch" if busy else "")
        for btn in (self.analyze_btn, self.clear_btn):
            btn.config(state="disabled" if busy else "normal")
        self.cancel_btn.config(state="normal" if busy else "disabled")
        self.export_btn.config(
            state="disabled" if busy or not self._last_report else "normal")
        self.import_run_btn.config(state="disabled" if busy else "normal")
        self.update_idletasks()

    def _populate(self, oligos, report: te.AnalysisReport):
        # Register the entire replacement view before a synchronous sub-job can
        # complete an import transaction belonging to the previous job set.
        self._render_build_depth = getattr(self, "_render_build_depth", 0) + 1
        try:
            self._populate_results(oligos, report)
        finally:
            self._render_build_depth -= 1

    def _populate_results(self, oligos, report: te.AnalysisReport):
        if not report.complete or report.phase != "complete":
            raise te.AnalysisIncompleteError(
                f"cannot display non-final report phase '{report.phase}'")
        self._populate_generation += 1
        self._cancel_render_jobs()
        generation = self._populate_generation
        # Tm table
        self.tm_tree.delete(*self.tm_tree.get_children())
        for t in report.tms:
            o = t.oligo
            self.tm_tree.insert("", "end", values=(
                self._display_oligo_name(o.name), o.length, o.n_variants,
                o.gc_display(),
                f"{o.conc_nM:g}", t.tm_display(), t.tm_owczarzy_display(),
                o.seq))

        # Populate one panel per interaction kind. Each interaction contributes
        # ordinary ranked peer rows under the same table contract.
        groups = {"Hairpin": report.hairpins,
                  "Self-dimer": report.self_dimers,
                  "Hetero-dimer": report.hetero_dimers}
        for kind, panel in self._panels.items():
            panel["selected_structures"] = {
                id(panel["iid"].get(iid)) for iid in panel["tree"].selection()}
            panel["tree"].delete(*panel["tree"].get_children())
            panel["iid"].clear()
            def rows(panel=panel, interactions=groups[kind]):
                for interaction in interactions:
                    yield from self._insert_structure_steps(panel, interaction)
                yield from _autosize_tree_steps(
                    panel["tree"], _STRUCTURE_COLUMN_BOUNDS)
            self._run_render_steps(
                kind, rows(), sum(len(s.structures) for s in groups[kind]))
        self._clear_detail()

        # Summary bar
        np, nc = len(report.problems), len(report.cautions)
        if np:
            sev = "problem"
            msg = self._t("status.problems", problems=np, cautions=nc)
        elif nc:
            sev = "caution"
            msg = self._t("status.no_problems", cautions=nc)
        else:
            sev = "ok"
            msg = self._t("status.no_liabilities")
        self._set_summary(f'{self._t("status.analysis_current")} {msg}', sev)
        self.engine_health.config(text=self._engine_health_text(report))
        self._refresh_overview(oligos, report)
        self._refresh_problems_first(report)
        # Both the delayed entry point and bounded cell batches reject obsolete
        # report generations before touching any Tk widget.
        self._render_jobs["matrix-entry"] = self.after_idle(
            lambda: self._populate_matrix_if_current(
                generation, oligos, report))

    def _populate_matrix_if_current(self, generation, oligos, report):
        if generation != self._populate_generation:
            return
        getattr(self, "_render_jobs", {}).pop("matrix-entry", None)
        try:
            self._draw_hetero_matrix(oligos, report)
        except Exception as error:
            self._fail_render_jobs(error)
            return
        self._finish_render_jobs()

    def _fail_render_jobs(self, error):
        self._cancel_render_jobs()
        self._populate_generation += 1
        recovery = getattr(self, "_render_recovery", None)
        failure_kind = getattr(self, "_render_failure_kind", None)
        self._render_recovery = None
        self._render_failure_kind = None
        self._render_on_complete = None
        technical = f"{type(error).__name__}: {error}"
        if recovery is not None:
            try:
                if failure_kind == "analysis":
                    self._last_report = recovery["report"]
                    self._last_oligos = recovery["oligos"]
                    self._last_cond = recovery["condition"]
                    if recovery["report"] is not None:
                        self._populate(recovery["oligos"], recovery["report"])
                        if recovery["detail"] is not None:
                            self._show_structure_detail(recovery["detail"])
                    else:
                        self._clear_result_tables()
                        self._reset_overview()
                else:
                    self._restore_analyzed_run_state(recovery)
            except Exception as restore_error:
                technical += ("\nUI restoration diagnostics: "
                              f"{type(restore_error).__name__}: {restore_error}")
        if failure_kind == "analysis":
            self._finish_analysis_error(type(error).__name__, technical)
            return
        messagebox.showerror(self._t("archive.import_failed"), self._t(
            "archive.apply_failed_body", error=technical))

    def _finish_render_jobs(self):
        if (not getattr(self, "_render_jobs", {})
                and not getattr(self, "_render_build_depth", 0)):
            self._render_recovery = None
            self._render_failure_kind = None
            complete = getattr(self, "_render_on_complete", None)
            self._render_on_complete = None
            if complete is not None:
                complete()

    def _cancel_render_jobs(self):
        for callback in getattr(self, "_render_jobs", {}).values():
            if callback is not None:
                self.after_cancel(callback)
        self._render_jobs = {}

    def _run_render_steps(self, key, steps, size):
        """Bound large Tk jobs to 8 ms slices; new jobs supersede the same view."""
        jobs = getattr(self, "_render_jobs", None)
        if jobs is None:
            self._render_jobs = jobs = {}
        previous = jobs.pop(key, None)
        if previous is not None:
            self.after_cancel(previous)
        if size < 200:
            for _ in steps:
                pass
            self._finish_render_jobs()
            return
        generation = self._populate_generation

        def advance():
            if generation != self._populate_generation or self._closing:
                jobs.pop(key, None)
                return
            deadline = time.monotonic() + 0.008
            try:
                while time.monotonic() < deadline:
                    next(steps)
            except StopIteration:
                jobs.pop(key, None)
                self._finish_render_jobs()
                return
            except Exception as error:
                # Deferred render failures use the same transactional restoration
                # as synchronous imports, instead of leaving a partial view.
                self._fail_render_jobs(error)
                return
            jobs[key] = self.after(1, advance)

        jobs[key] = self.after(1, advance)

    def _refresh_overview(self, oligos, report: te.AnalysisReport):
        findings = te.sorted_report_findings(te.iter_report_findings(report))
        problems = [f for f in findings if f.severity == "problem"]
        cautions = [f for f in findings if f.severity == "caution"]
        prime_risks = [
            f for f in findings
            if f.severity in {"problem", "caution"} and f.involves_3prime
        ]
        if problems:
            verdict = self._t("overview.review_problems")
        elif cautions:
            verdict = self._t("overview.review_cautions")
        else:
            verdict = self._t("overview.looks_clean")

        self._overview_vars["verdict"].set(verdict)
        self._overview_vars["problems"].set(str(len(problems)))
        self._overview_vars["cautions"].set(str(len(cautions)))
        self._overview_vars["prime"].set(str(len(prime_risks)))
        self._overview_vars["worst"].set(str(len(findings)))

        flagged = problems + cautions
        if not flagged:
            self._top_risks_var.set(
                self._t("overview.no_flagged", count=len(oligos)))
            return
        top_items = [
            self._t(
                "overview.risk_item",
                label=self._display_structure_label(f.label, f.kind),
                severity=self._display_severity(f.severity),
                site=self._display_site(f.involves_3prime), rank=f.rank)
            for f in flagged[:2]
        ]
        remaining = len(flagged) - len(top_items)
        if remaining > 0:
            top_items.append(self._t("overview.more", count=remaining))
        top = "; ".join(top_items)
        self._top_risks_var.set(self._t("overview.top_risks", items=top))

    def _reset_overview(self):
        defaults = {
            "verdict": self._t("overview.not_analyzed"),
            "problems": "0",
            "cautions": "0",
            "prime": "0",
            "worst": "-",
        }
        for key, value in defaults.items():
            if key in self._overview_vars:
                self._overview_vars[key].set(value)
        self._top_risks_var.set(
            self._t("overview.top_risks_idle"))

    def _clear_problem_filters(self):
        self._suspend_problem_refresh = True
        try:
            for key, var in self._filter_vars.items():
                if key == "search":
                    var.set("")
                else:
                    var.set(self._display_option(_FILTER_OPTIONS[key][0]))
            self._prime_risks_only_var.set(False)
            self._problems_sort_column = None
            self._problems_sort_desc = True
        finally:
            self._suspend_problem_refresh = False
        self._refresh_problems_first()

    def _refresh_problems_on_filter_change(self) -> None:
        if not getattr(self, "_suspend_problem_refresh", False):
            self._refresh_problems_first()

    def _autosize_problem_columns(self) -> None:
        if self._problems_panel is not None:
            _autosize_tree_columns(
                self._problems_panel["tree"], _FLAGGED_COLUMN_BOUNDS)

    @staticmethod
    def _fmt_optional(value) -> str:
        return "-" if value is None else f"{value:.2f}"

    def _engine_health_text(self, report: te.AnalysisReport) -> str:
        if not report.complete:
            return self._t("status.external_pending")
        manifest = report.manifest
        metadata = {} if manifest is None else manifest.to_dict().get("engines", {})
        unavailable = [name for name in ("Primer3", "ViennaRNA", "RNAstructure", "seqfold")
                       if metadata.get(name, {}).get("status") != "available"]
        readiness = ("; ".join(self._t(
            "status.engine_unavailable", engine=name) for name in unavailable)
                     if unavailable else self._t("status.engine_ready"))
        coverage = gui_results.ensemble_coverage_text(
            report, _tr, self._language_code)
        return f"{readiness} · {coverage}" if coverage else readiness

    def _fmt_structure_tm(self, s=None) -> str:
        """Combine every supported finite Tm into one engine-labelled cell."""

        if s is None:
            # Preserve the historical class-level helper call used by tests and
            # downstream integrations; such calls retain English output.
            s, language = self, "en"
        else:
            language = getattr(self, "_language_code", "en")
        return gui_results.format_structure_tm(s, _tr, language)

    def _structure_external_dg(self, structure, engine: str) -> str:
        return gui_results.format_external_dg(
            structure, engine, _tr, self._language_code)

    @staticmethod
    def _problem_result_columns() -> tuple[str, ...]:
        return gui_results.problem_result_columns()

    @staticmethod
    def _structure_pair_count(structure) -> int | None:
        return gui_results.structure_pair_count(structure)

    @staticmethod
    def _problem_metric_values(structure) -> tuple[float | None, float | None]:
        return gui_results.problem_metric_values(structure)

    def _concise_structure_assessment(self, structure) -> str:
        severity = getattr(structure, "severity", "unclassified")
        verdict_key = (
            "assessment.concise.interfere" if severity == "problem"
            else "assessment.concise.acceptable"
            if severity in {"ok", "caution"}
            else "assessment.concise.unclassified"
        )
        fragments = [self._t(verdict_key)]
        minimum_dg, maximum_tm = self._problem_metric_values(structure)
        if minimum_dg is not None:
            fragments.append(self._t(
                "assessment.concise.min_dg", value=f"{minimum_dg:.2f}"))
        if getattr(structure, "kind", "") == "Hairpin" and maximum_tm is not None:
            fragments.append(self._t(
                "assessment.concise.max_tm", value=f"{maximum_tm:.2f}"))
        pair_count = self._structure_pair_count(structure)
        fragments.append(self._t(
            "assessment.concise.n",
            value="-" if pair_count is None else pair_count))
        return "; ".join(fragments)

    def _finding_row_values(self, finding: te.ReportFinding) -> tuple:
        values = list(gui_results.finding_row_values(
            finding, _tr, self._language_code,
            display_severity=self._display_severity,
            display_kind=self._display_kind,
            display_site=self._display_site,
            display_label=self._display_structure_label,
            display_role=self._display_structure_role,
            display_assessment=self._display_assessment,
        ))
        values[self._problem_result_columns().index("assessment")] = (
            self._concise_structure_assessment(finding.structure))
        return tuple(values)

    @staticmethod
    def _effective_problem_severity_filter(severity: str,
                                           prime_risks_only: bool) -> str:
        if prime_risks_only and severity == "Flagged":
            return "All"
        return severity

    @classmethod
    def _sort_problem_findings(cls, findings: list[te.ReportFinding],
                               column: str, descending: bool
                               ) -> list[te.ReportFinding]:
        return gui_results.sort_problem_findings(
            findings, column, descending)

    def _sort_problem_column(self, column: str):
        if self._problems_sort_column == column:
            self._problems_sort_desc = not self._problems_sort_desc
        else:
            self._problems_sort_column = column
            self._problems_sort_desc = True
        self._refresh_problems_first()

    def _update_problem_sort_ui(self) -> None:
        panel = self._problems_panel
        if panel is None:
            return
        tree = panel["tree"]
        active = self._problems_sort_column
        for column, key in panel["heading_keys"].items():
            tree.heading(
                column,
                text=gui_results.sort_heading_text(
                    self._t(key), column == active,
                    self._problems_sort_desc),
            )
        if active is None:
            text = self._t("sort.default")
        else:
            label = self._t(panel["heading_keys"][active])
            text = gui_results.sort_status_text(
                label, self._problems_sort_desc, self._language_code)
        panel["sort"].config(text=text)

    def _refresh_problems_first(self, report: te.AnalysisReport | None = None):
        panel = self._problems_panel
        if panel is None:
            return

        tree = panel["tree"]
        previous = getattr(self, "_render_jobs", {}).pop("problems", None)
        if previous is not None:
            self.after_cancel(previous)
        selected = {id(panel["iid"].get(iid)) for iid in tree.selection()}
        self._update_problem_sort_ui()
        tree.delete(*tree.get_children())
        panel["iid"].clear()
        self._clear_detail()

        display_report = report if report is not None else self._last_report
        if not display_report:
            panel["count"].config(text=self._t("filter.showing_zero"))
            panel["active"].config(text=self._t("filter.active_default"))
            self._autosize_problem_columns()
            self._finish_render_jobs()
            return

        all_findings = te.sorted_report_findings(
            te.iter_report_findings(display_report))
        severity = self._effective_problem_severity_filter(
            self._canonical_filter_value("severity"),
            self._prime_risks_only_var.get(),
        )
        filtered = te.filter_report_findings(
            all_findings,
            severity=severity,
            site="All",
            kind=self._canonical_filter_value("kind"),
        )
        search = self._filter_vars["search"].get().strip().lower()
        if search:
            filtered = [
                f for f in filtered
                if search in " ".join((
                    f.kind, f.label, f.site,
                    te.structure_origin(f.structure), f.assessment
                )).lower()
            ]
        if self._prime_risks_only_var.get():
            filtered = [
                f for f in filtered
                if f.involves_3prime
            ]
        if self._problems_sort_column:
            filtered = self._sort_problem_findings(
                filtered,
                self._problems_sort_column,
                self._problems_sort_desc,
            )
        panel["count"].config(
            text=self._t("filter.showing", shown=len(filtered),
                         total=len(all_findings)))
        panel["active"].config(text=self._problem_filter_summary(search))

        if not filtered:
            empty_values = [""] * len(self._problem_result_columns())
            empty_values[self._problem_result_columns().index("label")] = (
                self._t("table.no_match"))
            empty_values[
                self._problem_result_columns().index("assessment")] = (
                    self._t("table.no_match_hint"))
            iid = tree.insert(
                "", "end", tags=("empty",),
                values=tuple(empty_values))
            panel["iid"][iid] = None
            self._autosize_problem_columns()
            self._finish_render_jobs()
            return

        def rows():
            for finding in filtered:
                iid = tree.insert(
                    "", "end", tags=(finding.severity,),
                    values=self._finding_row_values(finding))
                panel["iid"][iid] = finding.structure
                if id(finding.structure) in selected:
                    tree.selection_add(iid)
                yield
            yield from _autosize_tree_steps(tree, _FLAGGED_COLUMN_BOUNDS)
        self._run_render_steps("problems", rows(), len(filtered))

    def _problem_filter_summary(self, search: str) -> str:
        parts = []
        for key, label in [
            ("severity", "filter.severity_part"),
            ("kind", "filter.kind_part"),
        ]:
            value = self._canonical_filter_value(key)
            default = _FILTER_OPTIONS[key][0]
            if value != default:
                parts.append(self._t(label, value=self._display_option(value)))
        if search:
            parts.append(self._t("filter.search_part", search=search))
        if self._prime_risks_only_var.get():
            parts.append(self._t("filter.prime_only_part"))
        if not parts:
            return self._t("filter.active_default")
        return self._t("filter.active_prefix") + " | ".join(parts)

    @staticmethod
    def _short_label(label: str, max_len: int = 17) -> str:
        return label if len(label) <= max_len else label[:max_len - 1] + "..."

    @staticmethod
    def _matrix_marker(finding: te.ReportFinding) -> str:
        return gui_exports.matrix_marker(finding)

    def _clear_matrix_panel(self):
        previous = getattr(self, "_render_jobs", {}).pop("matrix", None)
        if previous is not None:
            self.after_cancel(previous)
        self._matrix_cell_map.clear()
        self._matrix_rects.clear()
        self._matrix_selected = None
        self._matrix_report = None
        self._clear_detail()
        panel = self._matrix_panel
        if panel is None:
            return
        panel["canvas"].delete("all")
        panel["canvas"].configure(scrollregion=(0, 0, 0, 0))
        panel["status"].config(text=self._t("matrix.run_first"))

    def _refresh_matrix(self):
        if self._last_oligos is None or self._last_report is None:
            return
        self._draw_hetero_matrix(self._last_oligos, self._last_report)

    def _draw_hetero_matrix(self, oligos, report: te.AnalysisReport):
        selected = (self._matrix_selected
                    if getattr(self, "_matrix_report", None) is report else None)
        self._clear_matrix_panel()
        self._matrix_report = report
        panel = self._matrix_panel
        if panel is None:
            self._finish_render_jobs()
            return
        canvas = panel["canvas"]
        canvas.focus_set()
        names = [o.name for o in oligos]
        if not names or (not report.hetero_dimers and not report.self_dimers):
            self._show_empty_matrix(canvas)
            panel["status"].config(text=self._t("matrix.no_cells"))
            self._finish_render_jobs()
            return

        matrix = te.hetero_dimer_matrix(oligos, report)
        width, height = self._matrix_dimensions(len(names))

        canvas.create_rectangle(0, 0, width, height,
                                fill=_UI["surface"], outline="")
        self._draw_matrix_column_headers(canvas, names)
        def cells():
            for i, row_name in enumerate(names):
                self._draw_matrix_row_header(canvas, i, row_name)
                for j, col_name in enumerate(names):
                    cell = matrix.get((row_name, col_name))
                    self._draw_matrix_cell(canvas, i, j, cell)
                    yield
            canvas.configure(scrollregion=(0, 0, width, height))
            panel["status"].config(text=self._t("matrix.status_click"))
            if selected in self._matrix_cell_map:
                self._select_matrix_cell(selected)
        self._run_render_steps("matrix", cells(), len(names) ** 2)

    def _show_empty_matrix(self, canvas):
        canvas.create_text(
            _SPACE["xl"], _SPACE["xl"], anchor="nw", fill=_UI["muted"],
            text=self._t("matrix.empty"), font=_FONT["body"])
        canvas.configure(scrollregion=(0, 0, 520, 80))

    def _matrix_dimensions(self, name_count: int) -> tuple[int, int]:
        width = _DIMEN["matrix_label_w"] + _DIMEN["matrix_cell_w"] * name_count
        height = _DIMEN["matrix_header_h"] + _DIMEN["matrix_cell_h"] * name_count
        return width, height

    def _draw_matrix_column_headers(self, canvas, names):
        label_w = _DIMEN["matrix_label_w"]
        cell_w = _DIMEN["matrix_cell_w"]
        header_h = _DIMEN["matrix_header_h"]
        for j, name in enumerate(names):
            x = label_w + j * cell_w
            canvas.create_rectangle(x, 0, x + cell_w, header_h,
                                    fill=_UI["matrix_header_bg"],
                                    outline=_UI["matrix_grid"])
            canvas.create_text(x + cell_w / 2, header_h / 2,
                               text=self._short_label(
                                   self._display_oligo_name(name)),
                               fill=_UI["text"], width=cell_w - _SPACE["sm"],
                               font=_FONT["body"])

    def _draw_matrix_row_header(self, canvas, row: int, row_name: str):
        label_w = _DIMEN["matrix_label_w"]
        cell_h = _DIMEN["matrix_cell_h"]
        header_h = _DIMEN["matrix_header_h"]
        y = header_h + row * cell_h
        canvas.create_rectangle(0, y, label_w, y + cell_h,
                                fill=_UI["matrix_header_bg"],
                                outline=_UI["matrix_grid"])
        canvas.create_text(_SPACE["sm"], y + cell_h / 2, anchor="w",
                           fill=_UI["text"],
                           text=self._short_label(
                               self._display_oligo_name(row_name), 22),
                           font=_FONT["body"])

    def _filtered_matrix_finding(self, cell):
        finding = cell.finding if cell else None
        if (finding is not None and self._matrix_flagged_only_var.get()
                and finding.severity == "ok"):
            return None
        return finding

    def _draw_matrix_cell(self, canvas, row: int, col: int, cell):
        label_w = _DIMEN["matrix_label_w"]
        cell_w = _DIMEN["matrix_cell_w"]
        cell_h = _DIMEN["matrix_cell_h"]
        header_h = _DIMEN["matrix_header_h"]
        x = label_w + col * cell_w
        y = header_h + row * cell_h
        finding = self._filtered_matrix_finding(cell)
        fill = (_SEV_COLOR.get(finding.severity, _UI["surface"])
                if finding else _UI["surface"])
        text = self._matrix_marker(finding) if finding else "-"
        tags = ("matrix-cell", f"matrix-cell:{row}:{col}")
        rect = canvas.create_rectangle(
            x, y, x + cell_w, y + cell_h,
            fill=fill, outline=_UI["matrix_grid"], tags=tags)
        canvas.create_text(
            x + cell_w / 2, y + cell_h / 2,
            fill=_UI["text"], text=text, tags=tags,
            font=_FONT["body"])
        if finding is not None:
            self._matrix_cell_map[(row, col)] = finding
            self._matrix_rects[(row, col)] = rect

    def _matrix_cell_from_event(self, event) -> tuple[int, int] | None:
        canvas = self._matrix_panel["canvas"]
        item = canvas.find_closest(canvas.canvasx(event.x), canvas.canvasy(event.y))
        if not item:
            return None
        for tag in canvas.gettags(item[0]):
            if tag.startswith("matrix-cell:"):
                _prefix, i, j = tag.split(":")
                return int(i), int(j)
        return None

    def _matrix_click(self, event):
        event.widget.focus_set()
        cell = self._matrix_cell_from_event(event)
        if cell is not None:
            self._select_matrix_cell(cell)

    def _select_matrix_cell(self, cell: tuple[int, int]):
        finding = self._matrix_cell_map.get(cell)
        if finding is None or self._matrix_panel is None:
            return
        panel = self._matrix_panel
        canvas = panel["canvas"]
        canvas.delete("matrix-selection")
        rect = self._matrix_rects.get(cell)
        if rect is not None:
            x1, y1, x2, y2 = canvas.coords(rect)
            canvas.create_rectangle(
                x1 + 1, y1 + 1, x2 - 1, y2 - 1,
                outline=_UI["focus"], width=3, tags=("matrix-selection",))
        self._matrix_selected = cell
        dg = finding.effective_dg
        dg_engine = finding.effective_dg_engine
        dg_text = ("-" if dg is None else
                   self._t("unit.dg", value=f"{dg:.2f}")
                   + f" ({dg_engine})")
        panel["status"].config(
            text=self._t(
                "matrix.status_cell",
                label=self._display_structure_label(
                    finding.label, finding.kind),
                         severity=self._display_severity(finding.severity),
                         dg=dg_text,
                         rank=finding.rank))
        self._show_structure_detail(finding.structure)

    def _move_matrix_selection(self, di: int, dj: int):
        if not self._matrix_cell_map:
            return "break"
        if self._matrix_selected is None:
            self._select_matrix_cell(next(iter(self._matrix_cell_map)))
            return "break"
        i, j = self._matrix_selected
        keys = set(self._matrix_cell_map)
        for _ in range(100):
            i += di
            j += dj
            if (i, j) in keys:
                self._select_matrix_cell((i, j))
                break
            if i < 0 or j < 0:
                break
        return "break"

    def _matrix_diagram(self):
        if self._matrix_selected is None or self._matrix_panel is None:
            return
        finding = self._matrix_cell_map.get(self._matrix_selected)
        if finding is None:
            return
        try:
            sd.open_diagram_window(
                self.master, finding.structure, lang=self._language_code)
        except Exception as e:
            messagebox.showerror(
                self._t("dialog.diagram_title"),
                self._t("dialog.diagram_error",
                        error_type=type(e).__name__, error=e))

    def _clear_result_tables(self):
        self._populate_generation += 1
        self._cancel_render_jobs()
        self.tm_tree.delete(*self.tm_tree.get_children())
        if self._problems_panel is not None:
            panel = self._problems_panel
            panel["tree"].delete(*panel["tree"].get_children())
            panel["iid"].clear()
            panel["count"].config(text=self._t("filter.showing_zero"))
            panel["active"].config(text=self._t("filter.active_default"))
        self._clear_matrix_panel()
        for panel in self._panels.values():
            panel["tree"].delete(*panel["tree"].get_children())
            panel["iid"].clear()
        self._clear_detail()

    def _insert_structure(self, panel, interaction):
        for _ in self._insert_structure_steps(panel, interaction):
            pass

    def _insert_structure_steps(self, panel, interaction):
        if not interaction.structures:
            return
        tree = panel["tree"]
        parent = tree.insert(
            "", "end",
            text=self._display_structure_label(
                interaction.label, interaction.kind),
            open=False, tags=("group",))
        for s in interaction.structures:
            if panel.get("aggregate_columns"):
                minimum_dg, maximum_tm = self._problem_metric_values(s)
                values = (
                    self._display_severity(s.severity),
                    self._display_site(s.involves_3prime),
                    self._structure_pair_count(s),
                    self._fmt_optional(minimum_dg),
                    self._fmt_optional(maximum_tm),
                    s.rank,
                    self._display_structure_role(te.structure_origin(s)),
                    self._concise_structure_assessment(s),
                )
                row = tree.insert(
                    parent, "end", text=f"#{s.rank}", tags=(s.severity,),
                    values=values)
                panel["iid"][row] = s
                if id(s) in panel.get("selected_structures", ()):
                    tree.selection_add(row)
                yield
                continue
            dg = self._fmt_optional(s.dg_p3)
            vienna = "-" if s.dg_vienna is None else f"{s.dg_vienna:.2f}"
            values = [
                self._structure_pair_count(s), dg, vienna,
                self._structure_external_dg(s, "RNAstructure"),
            ]
            if panel["kind"] == "Hairpin":
                values.append(self._structure_external_dg(s, "seqfold"))
            values.extend((
                self._fmt_structure_tm(s),
                self._display_site(s.involves_3prime),
                self._display_assessment(s.assessment, s.kind),
            ))
            row = tree.insert(
                parent, "end", text=f"#{s.rank}", tags=(s.severity,),
                values=tuple(values))
            panel["iid"][row] = s
            if id(s) in panel.get("selected_structures", ()):
                tree.selection_add(row)
            yield

    @staticmethod
    def _finite_structure_tm(value) -> float | None:
        if isinstance(value, (int, float)) and math.isfinite(value):
            return value
        return None

    def _structure_engine_fragments(self, s) -> list[str]:
        """Format exact-geometry external metrics in engine order."""

        engine_fragments = []
        for engine, (dg_value, tm_value) in te.ee.engine_metrics(
                getattr(s, "engine_observations", ())).items():
            if dg_value is not None:
                engine_fragments.append(
                    f"dG {engine}="
                    + self._t("unit.dg", value=f"{dg_value:.2f}"))
            if tm_value is not None:
                engine_fragments.append(f"Tm {engine}={tm_value:.2f} C")
        return engine_fragments

    def _structure_tm_fragment(
        self, tm_p3: float | None, tm_vienna: float | None,
    ) -> str:
        if tm_p3 is not None and tm_vienna is not None:
            return self._t(
                "structure.tm_both", tm_p3=tm_p3, tm_vienna=tm_vienna)
        if tm_p3 is not None:
            return self._t("structure.tm_single", tm=tm_p3)
        if tm_vienna is not None:
            return self._t("structure.tm_fixed", tm=tm_vienna)
        return ""

    def _base_structure_header(
        self, s, *, role: str, label: str, vienna_text: str,
        tm_text: str, engine_fragments: list[str],
    ) -> tuple[str, bool]:
        """Select the stable Primer3/Vienna/engine-only header template."""

        if s.dg_p3 is None and s.dg_vienna is not None:
            head = self._t(
                "structure.header_fixed",
                kind=self._display_kind(s.kind), label=label, role=role,
                dg=s.dg_vienna, tm=tm_text,
                severity=self._display_severity(s.severity),
                site=self._display_site(s.involves_3prime),
            )
        elif s.dg_p3 is not None:
            head = self._t(
                "structure.header",
                kind=self._display_kind(s.kind), label=label, role=role,
                dg=s.dg_p3, vienna=vienna_text, tm=tm_text,
                severity=self._display_severity(s.severity),
                site=self._display_site(s.involves_3prime),
            )
        else:
            values = ("; ".join(engine_fragments)
                      or self._t("detail.engine_metrics_unavailable"))
            head = (f"{label} ({role}) | "
                    f"{values} | {self._display_site(s.involves_3prime)}")
            return head, False
        return head, True

    def _struct_header(self, s) -> str:
        """Render one peer structure with separate engine-owned metrics."""
        role = self._display_structure_role(te.structure_origin(s))
        label = self._display_structure_label(s.label, s.kind)
        vienna_text = ("" if s.dg_vienna is None else
                       self._t("structure.vienna", dg=s.dg_vienna))
        tm_p3 = self._finite_structure_tm(getattr(s, "tm_c", None))
        tm_vienna = self._finite_structure_tm(
            getattr(s, "tm_vienna_c", None))
        engine_fragments = self._structure_engine_fragments(s)
        head, append_engine_metrics = self._base_structure_header(
            s, role=role, label=label, vienna_text=vienna_text,
            tm_text=self._structure_tm_fragment(tm_p3, tm_vienna),
            engine_fragments=engine_fragments)
        if append_engine_metrics and engine_fragments:
            head += self._t("detail.engine_metrics") + ": "
            head += "; ".join(engine_fragments) + "\n"
        if getattr(s, "n_combos", 1) > 1 and s.representative_variant:
            head += self._t("structure.representative_variant",
                            count=s.n_combos, variant=s.representative_variant)
        return head

    def _panel_show(self, panel):
        sel = panel["tree"].selection()
        if not sel:
            return
        s = panel["iid"].get(sel[0])
        self._show_structure_detail(s)

    def _show_structure_detail(self, s):
        if s is None or self._detail_text is None:
            self._clear_detail()
            return

        self._set_detail_expanded(True)
        drawable = sd.is_drawable(s)
        role = self._display_structure_role(te.structure_origin(s))
        label = self._display_structure_label(s.label, s.kind)
        self._detail_title_var.set(label)
        self._detail_status_var.set(
            self._t("detail.status", role=role,
                    severity=self._display_severity(s.severity),
                    site=self._display_site(getattr(s, "involves_3prime", False))))
        self._detail_text.config(state="normal")
        self._detail_text.delete("1.0", "end")
        if drawable:
            self._detail_structure = s
            if self._detail_diagram_btn is not None:
                self._detail_diagram_btn.config(state="normal")
            content = self._struct_header(s) + "\n" + sd.display_ascii(s)
        else:
            self._detail_structure = None
            if self._detail_diagram_btn is not None:
                self._detail_diagram_btn.config(state="disabled")
            content = self._t(
                "detail.empty_body",
                kind=self._display_kind(s.kind), label=label,
                assessment=self._display_assessment(s.assessment, s.kind))
        self._detail_text.insert("end", content)
        self._set_detail_expanded(
            True, rendered_lines=len(content.splitlines()))
        self._detail_text.config(state="disabled")

    def _set_detail_expanded(
            self, expanded: bool, rendered_lines: int | None = None):
        if self._detail_frame is not None:
            title = self._t("detail.selected" if expanded else "detail.none_title")
            self._detail_frame.configure(text=title)
        if self._detail_text is not None:
            if expanded:
                height = min(14, max(7, rendered_lines or 7))
            else:
                height = 3
            self._detail_text.configure(height=height)

    def _clear_detail(self):
        self._detail_structure = None
        self._detail_title_var.set(self._t("detail.none_selected"))
        self._detail_status_var.set(self._t("detail.select_prompt"))
        if self._detail_diagram_btn is not None:
            self._detail_diagram_btn.config(state="disabled")
        if self._detail_text is not None:
            self._set_detail_expanded(False)
            self._detail_text.config(state="normal")
            self._detail_text.delete("1.0", "end")
            self._detail_text.config(state="disabled")

    def _show_selected_diagram(self):
        s = self._detail_structure
        drawable = sd.is_drawable(s)
        if not drawable:
            messagebox.showinfo(self._t("dialog.diagram_title"),
                                self._t("dialog.diagram_body"))
            return
        try:
            sd.open_diagram_window(self.master, s, lang=self._language_code)
        except Exception as e:
            messagebox.showerror(
                self._t("dialog.diagram_title"),
                self._t("dialog.diagram_error",
                        error_type=type(e).__name__, error=e))

    def _panel_diagram(self, panel):
        sel = panel["tree"].selection()
        if not sel:
            return
        s = panel["iid"].get(sel[0])
        drawable = sd.is_drawable(s)
        if not drawable:
            messagebox.showinfo(self._t("dialog.diagram_title"),
                                self._t("dialog.diagram_body"))
            return
        try:                        # matplotlib is loaded lazily inside here
            sd.open_diagram_window(self.master, s, lang=self._language_code)
        except Exception as e:
            messagebox.showerror(
                self._t("dialog.diagram_title"),
                self._t("dialog.diagram_error",
                        error_type=type(e).__name__, error=e))

    def _export(self, kind: str = "text"):
        if not (self._last_report and self._last_oligos and self._last_cond):
            return
        if kind == "run_archive":
            gui_exports.save_analyzed_run(
                self._last_oligos, self._last_report, self._last_cond,
                _tr, self._language_code)
            return
        gui_exports.save_export(
            kind, self._last_oligos, self._last_report, self._last_cond,
            _tr, self._language_code,
            matrix_flagged_only=(
                self._matrix_flagged_only_var.get()
                if kind == "matrix_csv" else False),
        )

    @staticmethod
    def _bulk_projection(oligos) -> str:
        return "\n".join(f"{oligo.name} = {oligo.seq}" for oligo in oligos)

    def _confirm_analyzed_run_import(self, loaded) -> bool:
        """Show a validated archive summary before mutating any application state."""
        report = loaded.report
        coverage = report.ensemble_coverage
        manifest = report.manifest
        if coverage is None or manifest is None:
            return False  # Defensive: the bounded decoder already rejects this.
        focus_return = self.master.focus_get()
        result = {"import": False}
        win = tk.Toplevel(self.master)
        win.title(self._t("archive.preview_title"))
        win.transient(self.master)
        win.resizable(False, False)
        apply_window_icon(win)
        frame = ttk.Frame(win, padding=_SPACE["lg"], style="Lab.TFrame")
        frame.grid(sticky="nsew")
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text=self._t("archive.preview_heading"),
                  style="MetricValue.TLabel").grid(row=0, column=0,
                                                     columnspan=2, sticky="w")
        conditions = loaded.conditions
        enabled = loaded.ensemble_mode == te.ENSEMBLE_MODE_BUDGETED_COMPLETE
        mode = (("включён" if enabled else "выключен")
                if self._language_code == "ru" else
                ("on" if enabled else "off"))
        body = self._t(
            "archive.preview_body", oligos=len(loaded.oligos),
            conditions=(f"Mg2+ {conditions.dv_conc:g} mM; "
                        f"primer {conditions.primer_conc:g} nM; "
                        f"probe {conditions.probe_conc:g} nM"),
            mode=mode, budget=loaded.ensemble_additional_budget,
            evaluated=coverage.evaluated_contexts,
            total=coverage.total_contexts,
            completeness=self._t(
                "archive.preview_complete" if coverage.ensemble_complete
                else "archive.preview_partial"),
            version=manifest.application_version,
            policy=manifest.scientific_policy_version,
            analysis_id=manifest.analysis_id)
        if manifest.scientific_policy_version != te.sm.SCIENTIFIC_POLICY_VERSION:
            body += "\n\n" + self._t("archive.preview_historical_policy")
        ttk.Label(frame, text=body, justify="left", wraplength=560,
                  style="Lab.TLabel").grid(row=1, column=0, columnspan=2,
                                            sticky="ew", pady=_SPACE["md"])

        def close(import_run=False):
            result["import"] = bool(import_run)
            win.grab_release()
            win.destroy()
            if focus_return is not None:
                try:
                    focus_return.focus_set()
                except tk.TclError:
                    pass

        cancel = ttk.Button(frame, text=self._t("action.cancel"),
                            command=close, style="Secondary.TButton")
        cancel.grid(row=2, column=0, sticky="e", padx=(0, _SPACE["xs"]))
        confirm = ttk.Button(
            frame, text=self._t("archive.confirm_import"),
            command=lambda: close(True), style="Accent.TButton")
        confirm.grid(row=2, column=1, sticky="e")
        win.bind("<Escape>", lambda _event: close())
        win.bind("<Return>", lambda _event: close(True))
        win.protocol("WM_DELETE_WINDOW", close)
        win.grab_set()
        confirm.focus_set()
        self.master.wait_window(win)
        return result["import"]

    def _import_analyzed_run(self):
        if self._analysis_running():
            return
        path = gui_exports.choose_analyzed_run_path(_tr, self._language_code)
        if not path:
            return
        self._import_generation += 1
        generation = self._import_generation
        inputs = self._archive_input_signature()
        self._import_cancel = threading.Event()
        result_queue = queue.Queue(maxsize=1)
        self._set_busy(True)
        self._start_progress_pulse()
        self.progress_label.config(text=self._t("archive.open_title"))
        self._import_thread = threading.Thread(
            target=self._archive_import_worker,
            args=(path, self._import_cancel, result_queue),
            name="analyzed-run-import", daemon=False)
        self._import_thread.start()
        self.after(25, lambda: self._poll_archive_import(
            generation, inputs, path, result_queue))

    def _archive_input_signature(self):
        return (
            tuple((key, value.get()) for key, value in self._seq_vars.items()),
            self.bulk_text.get("1.0", "end-1c"), self.input_nb.select(),
            tuple((key, value.get()) for key, value in self._cond_vars.items()),
            self._ensemble_complete_var.get(), self._ensemble_budget_var.get(),
        )

    @staticmethod
    def _archive_import_worker(path, cancel_event, result_queue):
        try:
            loaded = analyzed_run_archive.load_analyzed_run_archive(
                path, cancel_event=cancel_event)
        except analyzed_run_archive.ArchiveLoadCancelled:
            result_queue.put(("cancelled", None))
        except Exception as error:
            result_queue.put(("error", error.with_traceback(None)))
        else:
            result_queue.put(("done", loaded))

    def _poll_archive_import(self, generation, inputs, path, result_queue):
        if generation != self._import_generation:
            return
        changed = inputs != self._archive_input_signature()
        if changed or self._closing:
            self._import_cancel.set()
        if self._import_thread.is_alive():
            self.after(25, lambda: self._poll_archive_import(
                generation, inputs, path, result_queue))
            return
        self._import_thread.join()  # Already stopped: never waits on the Tk thread.
        self._import_thread = None
        kind, payload = result_queue.get_nowait()
        cancelled = self._import_cancel.is_set() or kind == "cancelled"
        self._stop_progress_pulse()
        self._set_busy(False)
        if cancelled:
            self.progress_label.config(text=self._t("progress.cancelled"))
            return
        if kind == "error":
            self.progress_label.config(text=self._t("progress.failed"))
            error = (gui_exports.localized_persistence_error(
                payload, self._language_code)
                if isinstance(payload, gui_exports.PersistenceError)
                else gui_exports.localized_save_error(
                    path, payload, self._language_code))
            messagebox.showerror(self._t("archive.import_failed"), self._t(
                "archive.import_failed_body", path=path, error=error))
            return
        self._commit_analyzed_run(payload)

    def _commit_analyzed_run(self, loaded):
        if (hasattr(self, "master")
                and not self._confirm_analyzed_run_import(loaded)):
            return
        snapshot = self._capture_analyzed_run_state()
        self._render_failure_kind = "archive"
        self._render_on_complete = None
        self._render_recovery = snapshot
        try:
            self._apply_analyzed_run_state(loaded)
        except Exception as error:
            self._render_recovery = None
            self._render_on_complete = None
            restore_error = None
            try:
                self._restore_analyzed_run_state(snapshot)
            except Exception as recovery_error:
                restore_error = recovery_error
            technical = f"{type(error).__name__}: {error}"
            if restore_error is not None:
                technical += (
                    "\nUI restoration diagnostics: "
                    f"{type(restore_error).__name__}: {restore_error}")
            messagebox.showerror(
                self._t("archive.import_failed"),
                self._t("archive.apply_failed_body", error=technical))
            return
        def imported():
            messagebox.showinfo(
                self._t("archive.imported"), self._t("archive.imported_body"))
        if getattr(self, "_render_jobs", {}):
            self._render_on_complete = imported
        else:
            self._render_recovery = None
            imported()

    def _capture_analyzed_run_state(self) -> dict:
        return {
            "seq": {key: value.get() for key, value in self._seq_vars.items()},
            "bulk": self.bulk_text.get("1.0", "end-1c"),
            "input_tab": self.input_nb.select(),
            "conditions": {key: value.get()
                           for key, value in self._cond_vars.items()},
            "preset": self._condition_preset_var.get(),
            "ensemble_complete": self._ensemble_complete_var.get(),
            "ensemble_budget": self._ensemble_budget_var.get(),
            "report": self._last_report,
            "oligos": self._last_oligos,
            "condition": self._last_cond,
            "detail": self._detail_structure,
            "results_tab": self.results_nb.select(),
            "export_state": str(self.export_btn.cget("state")),
            "summary": (
                self.summary.cget("text"), self.summary.cget("background"),
                self.summary.cget("foreground")),
            "progress": (
                self.progress_value.get(), self.progress.cget("maximum"),
                self.progress_label.cget("text")),
        }

    def _set_analyzed_run_conditions(self, conditions) -> None:
        self._updating_conditions = True
        try:
            for key, variable in self._cond_vars.items():
                variable.set(f"{getattr(conditions, key):g}")
        finally:
            self._updating_conditions = False

    def _apply_analyzed_run_state(self, loaded) -> None:
        self._populate(list(loaded.oligos), loaded.report)
        self._set_analyzed_run_conditions(loaded.conditions)
        matched = self._matching_condition_preset() or "Custom"
        self._condition_preset_var.set(self._display_condition_preset(matched))
        for variable in self._seq_vars.values():
            variable.set("")
        self.bulk_text.delete("1.0", "end")
        self.bulk_text.insert("1.0", self._bulk_projection(loaded.oligos))
        self.input_nb.select(1)
        self._ensemble_complete_var.set(
            loaded.ensemble_mode == te.ENSEMBLE_MODE_BUDGETED_COMPLETE)
        self._ensemble_budget_var.set(str(loaded.ensemble_additional_budget))
        self._update_condition_summary()
        self._update_bulk_count()
        self._last_report = loaded.report
        self._last_oligos = list(loaded.oligos)
        self._last_cond = loaded.conditions
        total_steps = len(loaded.oligos) + loaded.report.ensemble_plan.allocated_contexts
        self.progress.configure(maximum=max(1, total_steps))
        self.progress_value.set(total_steps)
        self.progress_label.config(text=self._t("progress.complete"))
        self.export_btn.config(state="normal")

    def _restore_analyzed_run_state(self, snapshot: dict) -> None:
        # Invalidate a matrix callback queued by the failed candidate before
        # restoring the previous report (or clearing an empty UI).
        self._populate_generation += 1
        self._updating_conditions = True
        try:
            for key, value in snapshot["conditions"].items():
                self._cond_vars[key].set(value)
        finally:
            self._updating_conditions = False
        self._condition_preset_var.set(snapshot["preset"])
        for key, value in snapshot["seq"].items():
            self._seq_vars[key].set(value)
        self.bulk_text.delete("1.0", "end")
        self.bulk_text.insert("1.0", snapshot["bulk"])
        self.input_nb.select(snapshot["input_tab"])
        self._ensemble_complete_var.set(snapshot["ensemble_complete"])
        self._ensemble_budget_var.set(snapshot["ensemble_budget"])
        self._last_report = snapshot["report"]
        self._last_oligos = snapshot["oligos"]
        self._last_cond = snapshot["condition"]
        try:
            self._restore_analyzed_run_results(snapshot)
        finally:
            self.summary.config(
                text=snapshot["summary"][0],
                background=snapshot["summary"][1],
                foreground=snapshot["summary"][2])
            self.progress_value.set(snapshot["progress"][0])
            self.progress.configure(maximum=snapshot["progress"][1])
            self.progress_label.config(text=snapshot["progress"][2])
            self.export_btn.config(state=snapshot["export_state"])
            self._update_condition_summary()
            self._update_bulk_count()

    def _restore_analyzed_run_results(self, snapshot: dict) -> None:
        if snapshot["report"] is not None and snapshot["oligos"] is not None:
            self._populate(snapshot["oligos"], snapshot["report"])
            if snapshot["detail"] is not None:
                self._show_structure_detail(snapshot["detail"])
        else:
            self._clear_result_tables()
            self._reset_overview()
            self._clear_detail()
        self.results_nb.select(snapshot["results_tab"])

    def _has_input_or_results(self) -> bool:
        if self._last_report is not None:
            return True
        if any(v.get().strip() for v in self._seq_vars.values()):
            return True
        return bool(self.bulk_text.get("1.0", "end").strip())

    def _clear(self):
        if self._analysis_running():
            self.cancel_analysis()
            return
        if self._has_input_or_results():
            if not messagebox.askyesno(
                    self._t("dialog.clear_title"),
                    self._t("dialog.clear_body")):
                return
        for v in self._seq_vars.values():
            v.set("")
        self.bulk_text.delete("1.0", "end")
        self._update_bulk_count()
        self._clear_result_tables()
        self._render_recovery = None
        self._render_on_complete = None
        self._reset_overview()
        self._set_summary(self._t("status.enter_sequences"), "idle")
        self.export_btn.config(state="disabled")
        self._stop_progress_pulse()
        self._progress_has_completed_work = False
        self._progress_cancel_requested = False
        self._last_analysis_progress = None
        self.progress_value.set(0)
        self.progress.configure(maximum=1)
        self.progress_label.config(text=self._t("progress.idle"))
        self._last_report = None
        self._last_oligos = None
        self._last_cond = None


def main(root: tk.Tk | None = None) -> None:
    if root is None:
        te.ee.require_mandatory_engines()
        root = tk.Tk()
        preflight_thread = None
        preflight_failure: list[BaseException] = []
    else:
        preflight_failure = []

        def validate_engines() -> None:
            try:
                te.ee.require_mandatory_engines()
            except BaseException as exc:
                preflight_failure.append(exc)

        # Frozen startup already owns a hidden Tk root.  Engine attestation and
        # widget construction are independent, so overlap them before display.
        preflight_thread = threading.Thread(
            target=validate_engines,
            name="mandatory-engine-preflight",
            daemon=True,
        )
        preflight_thread.start()
    try:
        root.title(PRODUCT_NAME)
        root.geometry("1180x760")
        root.minsize(1020, 660)
        MBUprimeStructLabApp(root)
        if preflight_thread is not None:
            preflight_thread.join()
            if preflight_failure:
                raise preflight_failure[0]
        root.deiconify()
        root.mainloop()
    finally:
        if preflight_thread is not None and preflight_thread.is_alive():
            preflight_thread.join()


if __name__ == "__main__":
    main()
