"""Canonical-geometry diagrams for ranked hairpins and dimers.

Matplotlib rendering is schematic and preserves exact pair topology, strand
orientation, risk zones, and localized diagnostic text across PNG/PDF/SVG.
Plain-text structure formatting is delegated to ``headless_geometry`` as the
single authority; legacy ASCII parsing remains only a geometry fallback.
Matplotlib is imported lazily at the window/export boundary.
"""

from __future__ import annotations

import math
from pathlib import Path
import re

from app_assets import CASCADIA_MONO_NAME, apply_window_icon
from gui_tokens_generated import COLORS
from headless_geometry import (
    _parse_rows,
    _reconstruct_dimer,
    display_ascii as _headless_display_ascii,
    is_drawable as _headless_is_drawable,
)

# Colour per base (common convention: A green, C blue, G black/orange, T red).
BASE_COLORS = {
    "A": COLORS["diagram_base_a"],
    "C": COLORS["diagram_base_c"],
    "G": COLORS["diagram_base_g"],
    "T": COLORS["diagram_base_t"],
    "U": COLORS["diagram_base_t"],
    "N": COLORS["diagram_base_n"],
}
_BACKBONE_COLOR = COLORS["diagram_backbone"]
_BOND_NEUTRAL = COLORS["diagram_bond_neutral"]
_RISK_ZONE = COLORS["diagram_risk_zone"]
_TEXT = COLORS["diagram_text"]
_MUTED = COLORS["diagram_muted"]
_SEVERITY_STYLE = {
    "problem": ("PROBLEM", COLORS["problem_bg"], COLORS["problem_fg"],
                COLORS["diagram_problem_dark"]),
    "caution": ("CAUTION", COLORS["caution_bg"],
                COLORS["diagram_caution_accent"],
                COLORS["diagram_caution_dark"]),
    "ok": ("OK", COLORS["ok_bg"], COLORS["diagram_ok_accent"],
           COLORS["diagram_ok_dark"]),
    "unclassified": ("UNCLASSIFIED", COLORS["diagram_unclassified_bg"],
                     COLORS["diagram_bond_neutral"],
                     COLORS["diagram_unclassified_dark"]),
}
_DRAW_I18N = {
    "en": {
        "caution": "CAUTION",
        "diagnostic": "Diagnostic",
        "display": "Display",
        "export": "Export",
        "fixed_tm": "Fixed-structure dG=0 Tm {tm:.1f} C",
        "hairpin": "Hairpin",
        "hetero_dimer": "Hetero-dimer",
        "internal_site": "internal site",
        "kind_structure": "Structure",
        "pairs_metric": "Pairs {count} bp",
        "paired_bases": "{count} paired bases",
        "prime_risk": "3' risk",
        "problem": "PROBLEM",
        "ok": "OK",
        "save_diagram": "Save diagram",
        "save_failed": "Save failed",
        "save_error_access": (
            "Windows denied write access. The folder may be read-only or protected "
            "by Controlled Folder Access. Choose a writable folder or allow this "
            "application in Windows Security."),
        "save_error_disk_full": (
            "The destination disk has no free space. Free space or choose another drive."),
        "save_error_invalid_destination": (
            "The destination is missing or invalid. Choose an existing folder and a valid file name."),
        "save_error_busy": (
            "The file is busy or locked by another application. Close it there or choose another name."),
        "save_error_network_cloud": (
            "The network, cloud, or OneDrive destination is unavailable. Restore the "
            "connection/sync or save to a local folder."),
        "save_error_unexpected": (
            "An unexpected save error occurred. Choose another writable local folder and try again."),
        "save_error_path": "Selected path",
        "save_error_technical": "Technical details",
        "save_image": "Save image...",
        "saved": "Saved",
        "saved_body": "Diagram written to:\n{path}",
        "self_dimer": "Self-dimer",
        "site_3prime": "3' site",
        "stem_metric": "Stem {count} bp",
        "stem_note": "{count} bp stem",
        "structure_diagram": "Structure diagram",
        "tm": "Tm {tm:.1f} C",
        "tm_primer3": "Tm(Primer3) {tm:.1f} C",
        "tm_vienna": "Tm(Vienna fixed) {tm:.1f} C",
        "dg_vienna_fixed": "dG(Vienna fixed) {dg:.2f}",
    },
    "ru": {
        "caution": "ВНИМАНИЕ",
        "diagnostic": "Диагностика",
        "display": "Вид",
        "export": "Экспорт",
        "fixed_tm": "Tm dG=0 фикс. структуры {tm:.1f} C",
        "hairpin": "Шпилька",
        "hetero_dimer": "Гетеродимер",
        "internal_site": "внутри",
        "kind_structure": "Структура",
        "pairs_metric": "Пар {count}",
        "paired_bases": "Пар оснований: {count}",
        "prime_risk": "3' риск",
        "problem": "ПРОБЛЕМА",
        "ok": "OK",
        "save_diagram": "Сохранить схему",
        "save_failed": "Сохранение не выполнено",
        "save_error_access": (
            "Windows запретила запись. Папка может быть доступна только для чтения или "
            "защищена функцией «Контролируемый доступ к папкам». Выберите доступную для "
            "записи папку или разрешите приложение в Безопасности Windows."),
        "save_error_disk_full": (
            "На диске назначения нет свободного места. Освободите место или выберите другой диск."),
        "save_error_invalid_destination": (
            "Папка назначения отсутствует или имя файла недопустимо. Выберите существующую "
            "папку и корректное имя файла."),
        "save_error_busy": (
            "Файл занят или заблокирован другим приложением. Закройте его там или выберите другое имя."),
        "save_error_network_cloud": (
            "Сетевое, облачное или OneDrive-хранилище недоступно. Восстановите подключение "
            "или синхронизацию либо сохраните файл в локальную папку."),
        "save_error_unexpected": (
            "Произошла непредвиденная ошибка сохранения. Выберите другую доступную локальную "
            "папку и повторите попытку."),
        "save_error_path": "Выбранный путь",
        "save_error_technical": "Технические сведения",
        "save_image": "Сохранить...",
        "saved": "Сохранено",
        "saved_body": "Схема записана в:\n{path}",
        "self_dimer": "Гомодимер",
        "site_3prime": "3' конец",
        "stem_metric": "Основание {count}",
        "stem_note": "{count} п. осн.",
        "structure_diagram": "Схема структуры",
        "tm": "Tm {tm:.1f} C",
        "tm_primer3": "Tm(Primer3, модель двух состояний) {tm:.1f} C",
        "tm_vienna": "Tm(Vienna, фикс. структура) {tm:.1f} C",
        "dg_vienna_fixed": "dG(Vienna, фикс. структура) {dg:.2f}",
        "unclassified": "НЕ КЛАССИФИЦИРОВАНО",
    },
}

# Dimer diagram styling. The dimer figure is sized by the number of bases
# (inches per base) with a fixed height, and the axes stretch to fill it, so
# bases can be spaced out while the two strands stay clearly separated.
_DIMER_BASE_IN = 0.21     # horizontal room per base (inches)
_DIMER_FIG_H = 3.0        # dimer figure height (inches)
_BOND_LW = 2.8            # base-pair bond (rung) line width
_DIAGNOSTIC_HEADER_LEFT = 0.035
_DIAGNOSTIC_HEADER_RIGHT = 0.035
_DIAGNOSTIC_BADGE_Y = 0.83
_DIAGNOSTIC_TITLE_Y = 0.73
_DIAGNOSTIC_METRICS_Y = 0.65
_DIAGNOSTIC_PLOT_TOP = 0.53
_DIAGNOSTIC_METRIC_FONT_SIZE = 10.5
_EXPORT_TITLE_FONT_SIZE = 11.0
_HEADER_TEXT_WIDTH_SAFETY = 0.90
_NAVIEW_MIN_NORMALIZED_SEPARATION = 0.15
_FALLBACK_MIN_RADIUS = 1.25


def _dt(lang: str, key: str, **values) -> str:
    catalog = _DRAW_I18N.get(lang, _DRAW_I18N["en"])
    text = catalog.get(key, _DRAW_I18N["en"].get(key, key))
    return text.format(**values) if values else text


# --------------------------------------------------------------------------- #
# Hairpin
# --------------------------------------------------------------------------- #
def _pairs_from_pattern(pattern: str) -> list[tuple[int, int]]:
    """Pair '/' with matching '\\' via a stack (robust to bulges). Returns a
    list of (open_index, close_index) sorted by open index."""
    stack: list[int] = []
    pairs: list[tuple[int, int]] = []
    for i, ch in enumerate(pattern):
        if ch == "/":
            stack.append(i)
        elif ch == "\\":
            if stack:
                pairs.append((stack.pop(), i))
    pairs.sort()
    return pairs


def _naview_hairpin_layout(
        length: int, pairs: list[tuple[int, int]]) -> dict[int, tuple[float, float]]:
    """Lay out a noncrossing pair forest without flattening sibling stems."""
    import RNA

    dot_bracket = ["."] * length
    for left, right in pairs:
        dot_bracket[left], dot_bracket[right] = "(", ")"
    raw = RNA.naview_xy_coordinates("".join(dot_bracket))
    if len(raw) < length:
        raise RuntimeError("ViennaRNA NAVIEW returned too few hairpin coordinates")
    coordinates = [(float(point.X), float(point.Y)) for point in raw[:length]]
    if (not all(math.isfinite(value) for point in coordinates for value in point)
            or len(set(coordinates)) != length):
        raise RuntimeError("ViennaRNA NAVIEW returned invalid hairpin coordinates")
    steps = sorted(
        math.dist(first, second)
        for first, second in zip(coordinates, coordinates[1:])
        if math.dist(first, second) > 1e-9)
    if not steps:
        raise RuntimeError("ViennaRNA NAVIEW returned a collapsed hairpin backbone")
    scale = steps[len(steps) // 2]
    xs, ys = zip(*coordinates)
    center_x = (min(xs) + max(xs)) / 2.0
    center_y = (min(ys) + max(ys)) / 2.0
    normalized = {
        index: ((x - center_x) / scale, (center_y - y) / scale)
        for index, (x, y) in enumerate(coordinates)
    }
    minimum_separation = min(
        math.dist(normalized[first], normalized[second])
        for first in range(length)
        for second in range(first + 1, length))
    if minimum_separation < _NAVIEW_MIN_NORMALIZED_SEPARATION:
        raise RuntimeError("ViennaRNA NAVIEW returned overlapping coordinates")
    return normalized


def _circular_hairpin_fallback(length: int) -> dict[int, tuple[float, float]]:
    """Return a deterministic non-overlapping layout when NAVIEW is unavailable.

    Noncrossing hairpin bonds remain noncrossing as chords of the sequence
    circle, so the fallback preserves topology without guessing new pairs.
    """
    radius = max(_FALLBACK_MIN_RADIUS, length / (2.0 * math.pi))
    start_angle = -3.0 * math.pi / 4.0
    return {
        index: (
            radius * math.cos(start_angle + 2.0 * math.pi * index / length),
            radius * math.sin(start_angle + 2.0 * math.pi * index / length),
        )
        for index in range(length)
    }


def _hairpin_layout(seq: str, pattern: str) -> dict[int, tuple[float, float]]:
    """Assign an (x, y) coordinate to every residue index of a hairpin."""
    length = len(seq)
    pairs = _pairs_from_pattern(pattern)
    if not pairs:                    # no stem -> draw as a straight line
        return {index: (float(index), 0.0) for index in range(length)}
    try:
        return _naview_hairpin_layout(length, pairs)
    except Exception:
        # Diagram rendering is a presentation boundary. A missing or invalid
        # native layout must not make an otherwise valid analysis unusable.
        return _circular_hairpin_fallback(length)


def _draw_hairpin(ax, seq: str, pattern: str, sr,
                  mode: str = "diagnostic", lang: str = "en") -> None:
    pos = _hairpin_layout(seq, pattern)
    pairs = _pairs_from_pattern(pattern)
    L = len(seq)
    severity = _severity(sr)
    bond_color = _structure_bond_color(sr, risk=False, mode=mode)

    xs = [pos[i][0] for i in range(L)]
    ys = [pos[i][1] for i in range(L)]
    ax.plot(xs, ys, "-", color=_BACKBONE_COLOR, lw=1.2, zorder=1)

    for o, c in pairs:
        (x1, y1), (x2, y2) = pos[o], pos[c]
        lw = 2.0 if severity in ("problem", "caution") and mode == "diagnostic" else 1.4
        ax.plot([x1, x2], [y1, y2], "-", color=bond_color, lw=lw, zorder=2)

    _draw_bases(ax, seq, pos)
    _label_ends(ax, pos[0], pos[L - 1])
    if pairs and mode == "diagnostic":
        pair_ys = [pos[o][1] for o, _c in pairs]
        note_x = max(xs) + max(0.5, (max(xs) - min(xs)) * 0.08)
        ax.text(note_x, (min(pair_ys) + max(pair_ys)) / 2,
                _dt(lang, "stem_note", count=len(pairs)),
                ha="left", va="center", fontsize=10, color=_MUTED,
                bbox=dict(boxstyle="round,pad=0.24", fc="white",
                          ec=COLORS["diagram_node_outline"], lw=0.8),
                zorder=4)
    _finish_axes(ax, sr, mode=mode, pair_count=len(pairs), lang=lang)


# --------------------------------------------------------------------------- #
# Dimer (self / hetero)
# --------------------------------------------------------------------------- #
def _duplex_coordinates(offset: int):
    shift = max(0, -offset)
    return (lambda index: shift + index,
            lambda index: shift + offset + index)


def _duplex_risk_indices(top_length: int, bottom_length: int):
    return (
        {index for index in (top_length - 1, top_length - 2) if index >= 0},
        {index for index in (0, 1) if index < bottom_length},
    )


def _draw_duplex_backbones(ax, top_length: int, bottom_length: int,
                           xt, xb, y_top: float, y_bottom: float) -> None:
    ax.plot([xt(index) for index in range(top_length)],
            [y_top] * top_length, "-", color=_BACKBONE_COLOR, lw=1.3, zorder=1)
    ax.plot([xb(index) for index in range(bottom_length)],
            [y_bottom] * bottom_length, "-", color=_BACKBONE_COLOR,
            lw=1.3, zorder=1)


def _draw_duplex_bonds(ax, sr, pairs, xt, xb, top_risk: set[int],
                       bottom_risk: set[int], y_top: float,
                       y_bottom: float, mode: str) -> bool:
    risk_pair = False
    for top_index, bottom_index in pairs:
        is_risk = top_index in top_risk or bottom_index in bottom_risk
        risk_pair = risk_pair or is_risk
        color = _structure_bond_color(sr, risk=is_risk, mode=mode)
        width = ((_BOND_LW + 0.9)
                 if is_risk and mode == "diagnostic" else _BOND_LW)
        ax.plot([xt(top_index), xb(bottom_index)], [y_top, y_bottom], "-",
                color=color, lw=width, zorder=2, solid_capstyle="round")
    return risk_pair


def _draw_duplex_bases(ax, top: str, bottom: str, xt, xb,
                       y_top: float, y_bottom: float) -> None:
    for index, base in enumerate(top):
        _draw_base(ax, base, xt(index), y_top)
    for index, base in enumerate(bottom):
        _draw_base(ax, base, xb(index), y_bottom)


def _label_duplex_ends(ax, top_length: int, bottom_length: int, xt, xb,
                       y_top: float, y_bottom: float) -> None:
    for x, y, label in (
        (xt(0) - 1.2, y_top, "5'"),
        (xt(top_length - 1) + 1.2, y_top, "3'"),
        (xb(0) - 1.2, y_bottom, "3'"),
        (xb(bottom_length - 1) + 1.2, y_bottom, "5'"),
    ):
        ax.text(x, y, label, ha="center", va="center", fontsize=11,
                color=_MUTED)


def _annotate_duplex_pairs(ax, pairs, xt, risk_pair: bool, lang: str) -> None:
    x_mid = sum(xt(index) for index, _bottom in pairs) / len(pairs)
    note = _dt(lang, "paired_bases", count=len(pairs))
    if risk_pair:
        note += " - " + _dt(lang, "prime_risk")
    ax.text(x_mid, 1.72, note, ha="center", va="center", fontsize=10,
            color=_MUTED,
            bbox=dict(boxstyle="round,pad=0.24", fc="white",
                      ec=COLORS["diagram_node_outline"], lw=0.8),
            zorder=4)


def _render_duplex(ax, sr, top: str, bot: str, d: int, pairs,
                   mode: str = "diagnostic", lang: str = "en") -> None:
    """Shared renderer for every peer dimer diagram.

    Draws the two strands as full, contiguous rows -- top 5'->3' left to right,
    bottom antiparallel (already reversed to read 3'->5' left to right) -- offset
    by `d` so the paired region lines up, with red rungs at `pairs` = list of
    (top_index, bot_index). Because each strand is drawn contiguously there are
    no backbone gaps for bulges."""
    la, lb = len(top), len(bot)
    y_top, y_bot = 1.0, 0.0
    xt, xb = _duplex_coordinates(d)
    top_risk, bot_risk = _duplex_risk_indices(la, lb)
    if mode == "diagnostic":
        _shade_dimer_risk_zones(ax, xt, xb, top_risk, bot_risk, y_top, y_bot)
    _draw_duplex_backbones(ax, la, lb, xt, xb, y_top, y_bot)
    # Base-pair bonds first, so the white base circles mask their ends.
    risk_pair = _draw_duplex_bonds(
        ax, sr, pairs, xt, xb, top_risk, bot_risk, y_top, y_bot, mode)
    _draw_duplex_bases(ax, top, bot, xt, xb, y_top, y_bot)
    _label_duplex_ends(ax, la, lb, xt, xb, y_top, y_bot)
    if pairs and mode == "diagnostic":
        _annotate_duplex_pairs(ax, pairs, xt, risk_pair, lang)
    # Stretch to fill the (wide) figure so bases are spaced out and the two
    # strands stay well separated -- rather than the equal-aspect thin strip.
    ax.set_ylim(-1.4, 2.4)
    _finish_axes(ax, sr, aspect="auto", mode=mode,
                 pair_count=len(pairs), lang=lang)


def display_ascii(sr) -> str:
    """Detail-pane ASCII for any ranked peer structure, in one
    consistent style: hairpins as pattern-over-sequence (no SEQ/STR labels),
    dimers as the labelled two-strand duplex."""
    return _headless_display_ascii(sr)


def is_drawable(sr) -> bool:
    """Return whether a found structure has geometry that can be rendered."""
    return _headless_is_drawable(sr)


def _draw_dimer(ax, rows: list[tuple[str, str]], sr,
                mode: str = "diagnostic", lang: str = "en") -> None:
    """Reconstruct a tagged Primer3 dimer and draw the shared duplex style."""
    seq_rows = [c for t, c in rows if t == "SEQ"]
    str_rows = [c for t, c in rows if t == "STR"]
    if not seq_rows or not str_rows:                       # unexpected format
        ax.text(0.5, 0.5, sr.structure, family=CASCADIA_MONO_NAME,
                ha="center", va="center", transform=ax.transAxes)
        _finish_axes(ax, sr, mode=mode, lang=lang)
        return
    top, bot, pairs = _reconstruct_dimer(rows)
    # Offset so the first base pair lines up vertically (contiguous helices
    # then align exactly for every peer geometry).
    d = (pairs[0][0] - pairs[0][1]) if pairs else 0
    _render_duplex(ax, sr, top, bot, d, pairs, mode=mode, lang=lang)


# --------------------------------------------------------------------------- #
# Shared drawing helpers
# --------------------------------------------------------------------------- #
def _draw_base(ax, base: str, x: float, y: float) -> None:
    ax.text(x, y, base, ha="center", va="center", fontsize=11, fontweight="bold",
            color=BASE_COLORS.get(
                base.upper(), COLORS["diagram_base_fallback"]), zorder=3,
            bbox=dict(boxstyle="circle,pad=0.14", fc="white", ec="none"))


def _draw_bases(ax, seq: str, pos: dict[int, tuple[float, float]]) -> None:
    for i, b in enumerate(seq):
        x, y = pos[i]
        _draw_base(ax, b, x, y)


def _label_ends(ax, p5: tuple[float, float], p3: tuple[float, float]) -> None:
    ax.annotate("5'", p5, textcoords="offset points", xytext=(-18, -8),
                fontsize=11, color=_MUTED, ha="right", va="center")
    ax.annotate("3'", p3, textcoords="offset points", xytext=(18, -8),
                fontsize=11, color=_MUTED, ha="left", va="center")


def _shade_dimer_risk_zones(ax, xt, xb, top_risk: set[int],
                            bot_risk: set[int], y_top: float,
                            y_bot: float) -> None:
    from matplotlib.patches import Rectangle

    for i in top_risk:
        ax.add_patch(Rectangle((xt(i) - 0.46, y_top - 0.34), 0.92, 0.68,
                               facecolor=_RISK_ZONE, edgecolor="none",
                               zorder=0))
    for k in bot_risk:
        ax.add_patch(Rectangle((xb(k) - 0.46, y_bot - 0.34), 0.92, 0.68,
                               facecolor=_RISK_ZONE, edgecolor="none",
                               zorder=0))


def _severity(sr) -> str:
    severity = str(getattr(sr, "severity", "ok") or "ok").lower()
    return severity if severity in _SEVERITY_STYLE else "ok"


def _structure_bond_color(sr, risk: bool, mode: str) -> str:
    if mode == "export":
        return _BOND_NEUTRAL
    severity = _severity(sr)
    if risk or severity == "problem":
        return _SEVERITY_STYLE["problem"][2]
    if severity == "caution":
        return _SEVERITY_STYLE["caution"][2]
    return _BOND_NEUTRAL


def _kind(sr) -> str:
    return str(getattr(sr, "kind", "Structure"))


def _kind_text(sr, lang: str = "en") -> str:
    kind = _kind(sr)
    key = {
        "Hairpin": "hairpin",
        "Self-dimer": "self_dimer",
        "Hetero-dimer": "hetero_dimer",
    }.get(kind, "kind_structure")
    return _dt(lang, key)


def _label(sr, lang: str = "en") -> str:
    label = str(getattr(sr, "label", ""))
    kind = _kind(sr)
    if label.startswith(kind + ":"):
        label = _kind_text(sr, lang) + label[len(kind):]
    if lang == "ru":
        label = re.sub(r"(?<![A-Za-z])Oligo (\d+)", r"Олиго \1", label)
        for source, target in (
                ("Forward primer", "Прямой праймер"),
                ("Reverse primer", "Обратный праймер"),
                ("Probe 1", "Зонд 1"), ("Probe 2", "Зонд 2")):
            label = label.replace(source, target)
    return label


def _site_text(sr, lang: str = "en") -> str:
    return (_dt(lang, "site_3prime") if getattr(sr, "involves_3prime", False)
            else _dt(lang, "internal_site"))


def _finite_metric(value) -> float | None:
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _external_metrics(sr, quantity: str) -> list[tuple[str, float]]:
    values: dict[str, float] = {}
    attribute = "dg_kcal_mol" if quantity == "dg" else "tm_c"
    for observation in getattr(sr, "engine_observations", ()):
        value = _finite_metric(getattr(observation, attribute, None))
        if observation.status == "ok" and value is not None:
            values.setdefault(observation.engine, value)
    return [(engine, values[engine]) for engine in ("RNAstructure", "seqfold")
            if engine in values]


def _tm_metrics(sr, lang: str = "en") -> list[str]:
    metrics: list[str] = []
    tm_p3 = _finite_metric(getattr(sr, "tm_c", None))
    tm_vienna = _finite_metric(getattr(sr, "tm_vienna_c", None))
    if tm_p3 is not None:
        metrics.append(_dt(lang, "tm_primer3", tm=tm_p3))
    if tm_vienna is not None:
        metrics.append(_dt(lang, "tm_vienna", tm=tm_vienna))
    metrics.extend(
        f"Tm({engine}) {value:.1f} C"
        for engine, value in _external_metrics(sr, "tm"))
    return metrics


def _dg_metrics(sr, lang: str = "en") -> list[str]:
    metrics = []
    dg = getattr(sr, "dg_kcal", None)
    if dg is not None:
        metrics.append(f"dG(P3) {dg:.2f}")
    dg_v = getattr(sr, "dg_vienna", None)
    if dg_v is not None:
        if "fixed structure" in getattr(sr, "thermo_model", "").lower():
            metrics.append(_dt(lang, "dg_vienna_fixed", dg=dg_v))
        else:
            metrics.append(f"dG(Vienna) {dg_v:.2f}")
    metrics.extend(
        f"dG({engine}) {value:.2f}"
        for engine, value in _external_metrics(sr, "dg"))
    return metrics


def _metric_strip(sr, pair_count: int | None = None,
                  export: bool = False, lang: str = "en") -> str:
    metrics: list[str] = []
    if pair_count is not None:
        key = "stem_metric" if _kind(sr) == "Hairpin" else "pairs_metric"
        metrics.append(_dt(lang, key, count=pair_count))
    metrics.extend(_tm_metrics(sr, lang=lang))
    metrics.extend(_dg_metrics(sr, lang=lang))
    if _kind(sr) != "Hairpin":
        metrics.append(_site_text(sr, lang=lang))
    sep = "   |   " if not export else "  |  "
    return sep.join(metrics)


def _wrapped_metric_strip(sr, pair_count: int | None = None,
                          export: bool = False, lang: str = "en",
                          max_width_points: float | None = None,
                          font_size: float = _DIAGNOSTIC_METRIC_FONT_SIZE) -> str:
    """Wrap complete metric fields without letting them squeeze the plot."""
    separator = "  |  " if export else "   |   "
    fields = _metric_strip(
        sr, pair_count, export=export, lang=lang).split(separator)
    if max_width_points is None:
        max_width_points = 72.0 * 6.0
    lines: list[str] = []
    current = ""
    for field in fields:
        candidate = separator.join(filter(None, (current, field)))
        if current and _text_width_points(candidate, font_size) > max_width_points:
            lines.append(current)
            current = field
        else:
            current = candidate
    if current:
        lines.append(current)
    return "\n".join(lines)


def _text_width_points(text: str, font_size: float) -> float:
    """Measure text with Matplotlib's active sans-serif font."""
    try:
        from matplotlib.font_manager import FontProperties
        from matplotlib.textpath import TextPath

        path = TextPath((0.0, 0.0), text,
                        prop=FontProperties(family="sans-serif",
                                            size=font_size))
        return float(path.get_extents().width)
    except Exception:
        return len(text) * font_size * 0.6


def _available_header_width_points(ax) -> float:
    return (ax.figure.get_figwidth() * 72.0
            * (1.0 - _DIAGNOSTIC_HEADER_LEFT - _DIAGNOSTIC_HEADER_RIGHT)
            * _HEADER_TEXT_WIDTH_SAFETY)


def _wrapped_title(text: str, max_width_points: float,
                   font_size: float) -> str:
    """Wrap a localized title at words, falling back to safe chunks."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = " ".join(filter(None, (current, word)))
        if current and _text_width_points(candidate, font_size) > max_width_points:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return "\n".join(lines) if lines else text


def _draw_diagnostic_header(ax, sr, pair_count: int | None = None,
                            lang: str = "en") -> None:
    severity = _severity(sr)
    _badge, fill, edge, text = _SEVERITY_STYLE[severity]
    badge = _dt(lang, severity)
    transform = ax.figure.transFigure
    left = _DIAGNOSTIC_HEADER_LEFT
    ax.text(left, _DIAGNOSTIC_BADGE_Y, badge, transform=transform,
            ha="left", va="center", fontsize=10, fontweight="bold",
            color=text, clip_on=False, in_layout=False,
            bbox=dict(boxstyle="round,pad=0.30", fc=fill, ec=edge, lw=1.0))
    title = _wrapped_title(
        _label(sr, lang), _available_header_width_points(ax), 13)
    ax.text(left, _DIAGNOSTIC_TITLE_Y, title,
            transform=transform, ha="left", va="top", fontsize=13,
            color=_TEXT, clip_on=False, in_layout=False)
    metrics = _wrapped_metric_strip(
        sr, pair_count, lang=lang,
        max_width_points=_available_header_width_points(ax))
    if metrics:
        metrics_y = min(
            _DIAGNOSTIC_METRICS_Y,
            _DIAGNOSTIC_TITLE_Y - 0.055 * len(title.splitlines()))
        ax.text(left, metrics_y, metrics, transform=transform,
                ha="left", va="top", fontsize=_DIAGNOSTIC_METRIC_FONT_SIZE,
                color=_MUTED, clip_on=False, in_layout=False,
                linespacing=1.25)


def _finish_axes(ax, sr, aspect: str = "equal", mode: str = "diagnostic",
                 pair_count: int | None = None, lang: str = "en") -> None:
    ax.set_aspect(aspect)
    ax.axis("off")
    ax.margins(0.15)
    if mode == "export":
        title = _wrapped_title(
            _label(sr, lang), _available_header_width_points(ax),
            _EXPORT_TITLE_FONT_SIZE)
        metrics = _wrapped_metric_strip(
            sr, pair_count, export=True, lang=lang,
            max_width_points=_available_header_width_points(ax),
            font_size=_EXPORT_TITLE_FONT_SIZE)
        if metrics:
            title += f"\n{metrics}"
        ax.set_title(title, fontsize=_EXPORT_TITLE_FONT_SIZE, color=_TEXT)
    else:
        _draw_diagnostic_header(ax, sr, pair_count, lang=lang)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def _draw_duplex_sub(ax, sub, mode: str = "diagnostic",
                     lang: str = "en") -> None:
    """Draw a legacy enumerated duplex with the shared duplex renderer."""
    Br = sub.du_b[::-1]
    lb = len(Br)
    pairs = [(a_i, lb - 1 - b_j) for a_i, b_j in sub.du_pairs]
    _render_duplex(ax, sub, sub.du_a, Br, sub.du_offset, pairs,
                   mode=mode, lang=lang)


def render(ax, sr, mode: str = "diagnostic", lang: str = "en") -> None:
    """Render one ranked peer structure onto Axes *ax*."""
    geometry = getattr(sr, "canonical_geometry", None)
    if geometry is not None:
        if geometry.kind == "hairpin":
            pattern = ["-"] * len(geometry.sequence_a)
            for left, right in geometry.pairs:
                pattern[left], pattern[right] = "/", "\\"
            _draw_hairpin(
                ax, geometry.sequence_a, "".join(pattern), sr,
                mode=mode, lang=lang)
        else:
            bottom = (geometry.sequence_b or "")[::-1]
            bottom_length = len(bottom)
            display_pairs = [
                (left, bottom_length - 1 - right)
                for left, right in geometry.pairs]
            offset = (display_pairs[0][0] - display_pairs[0][1]
                      if display_pairs else 0)
            _render_duplex(
                ax, sr, geometry.sequence_a, bottom, offset,
                display_pairs, mode=mode, lang=lang)
        return
    if hasattr(sr, "mode"):                       # nn_thermo.SubStructure
        if sr.mode == "hairpin":
            _draw_hairpin(ax, sr.hp_seq, sr.hp_pattern, sr,
                          mode=mode, lang=lang)
        else:
            _draw_duplex_sub(ax, sr, mode=mode, lang=lang)
        return
    rows = _parse_rows(sr.structure)
    if sr.kind == "Hairpin" and len(rows) >= 2:
        pattern, seq = rows[0][1], rows[1][1]
        _draw_hairpin(ax, seq, pattern, sr, mode=mode, lang=lang)
    else:
        _draw_dimer(ax, rows, sr, mode=mode, lang=lang)


def _is_dimer(sr) -> bool:
    geometry = getattr(sr, "canonical_geometry", None)
    if geometry is not None:
        return geometry.kind != "hairpin"
    if hasattr(sr, "mode"):
        return sr.mode == "duplex"
    return sr.kind in ("Self-dimer", "Hetero-dimer")


def _dimer_ncols(sr) -> int:
    """Return the complete aligned duplex span used by the renderer."""
    geometry = getattr(sr, "canonical_geometry", None)
    if geometry is not None:
        la = len(geometry.sequence_a)
        lb = len(geometry.sequence_b or "")
        display_pairs = [
            (left, lb - 1 - right) for left, right in geometry.pairs]
        offset = (display_pairs[0][0] - display_pairs[0][1]
                  if display_pairs else 0)
        return max(la - 1, offset + lb - 1) - min(0, offset) + 1
    if hasattr(sr, "mode"):
        la, lb, offset = len(sr.du_a), len(sr.du_b), sr.du_offset
        return max(la - 1, offset + lb - 1) - min(0, offset) + 1
    rows = _parse_rows(sr.structure)
    return max((len(c) for _t, c in rows), default=20)


def figure_for(sr, mode: str = "diagnostic", lang: str = "en"):
    """Build a standalone matplotlib Figure for a ranked peer structure.

    Dimers get a wide, short figure sized by base count (so bases are spaced
    out and the strands stay separated); hairpins keep a square figure."""
    from matplotlib.figure import Figure
    if mode not in {"diagnostic", "export"}:
        raise ValueError("mode must be 'diagnostic' or 'export'")
    if _is_dimer(sr):
        n = _dimer_ncols(sr)
        width = min(max(n * _DIMER_BASE_IN + 1.6, 6.0), 40.0)
        fig = Figure(figsize=(width, _DIMER_FIG_H + 0.3), dpi=100)
    else:
        fig = Figure(figsize=(6.5, 6.3), dpi=100)
    ax = fig.add_subplot(111)
    render(ax, sr, mode=mode, lang=lang)
    if mode == "diagnostic":
        fig.tight_layout(rect=(0.0, 0.0, 1.0, _DIAGNOSTIC_PLOT_TOP))
    else:
        fig.tight_layout()
    return fig


def save_figure_transactionally(figure, path: str) -> None:
    """Atomically save one supported diagram format beside its destination."""

    extension = Path(path).suffix.lower().lstrip(".")
    if extension not in {"png", "pdf", "svg"}:
        raise ValueError("diagram file name must end in .png, .pdf, or .svg")
    # Lazy import avoids the thermo_engine -> structure_draw -> gui_exports
    # import cycle while keeping both GUI save boundaries on one implementation.
    from gui_exports import transactional_write

    transactional_write(
        path,
        lambda handle: figure.savefig(
            handle, format=extension, dpi=200, bbox_inches="tight"),
    )


def _diagram_save_error(path: str, exc: BaseException, lang: str) -> str:
    from gui_exports import classify_save_error

    category = classify_save_error(exc)
    return (
        f"{_dt(lang, f'save_error_{category}')}\n\n"
        f"{_dt(lang, 'save_error_path')}:\n{path}\n\n"
        f"{_dt(lang, 'save_error_technical')}:\n"
        f"{type(exc).__name__}: {exc}"
    )


def open_diagram_window(parent, sr, lang: str = "en") -> None:
    """Open a Toplevel window showing the diagram for *sr*, with a single
    display-mode selector and a 'Save image' button."""
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

    win = tk.Toplevel(parent)
    win.title(f"{_dt(lang, 'structure_diagram')} - {_label(sr, lang)}")
    apply_window_icon(win)
    opener = parent.focus_get() if hasattr(parent, "focus_get") else None

    def close_window(_event=None):
        win.destroy()
        if opener is not None:
            try:
                opener.focus_set()
            except tk.TclError:
                pass

    if hasattr(win, "bind"):
        win.bind("<Escape>", close_window)
    if hasattr(win, "protocol"):
        win.protocol("WM_DELETE_WINDOW", close_window)
    initial_fig = figure_for(sr, mode="diagnostic", lang=lang)
    intrinsic_width = math.ceil(initial_fig.get_figwidth() * initial_fig.dpi)
    screen_width = win.winfo_screenwidth()
    screen_height = win.winfo_screenheight()
    max_viewport_width = max(320, screen_width - 80)
    viewport_width = min(max(680, intrinsic_width), max_viewport_width)
    viewport_height = min(680, max(320, screen_height - 80))
    win.geometry(f"{viewport_width}x{viewport_height}")
    win.minsize(min(680, viewport_width), min(520, viewport_height))

    controls = ttk.Frame(win, padding=(8, 6, 8, 2))
    controls.pack(fill="x")
    ttk.Label(controls, text=_dt(lang, "display")).pack(side="left")
    diagnostic_label = _dt(lang, "diagnostic")
    export_label = _dt(lang, "export")
    mode_var = tk.StringVar(value=diagnostic_label)
    mode_box = ttk.Combobox(
        controls, textvariable=mode_var, state="readonly", width=13,
        values=(diagnostic_label, export_label))
    mode_box.pack(side="left", padx=(6, 12))

    figure_frame = ttk.Frame(win)
    figure_frame.pack(fill="both", expand=True)
    fig_ref = {"fig": None, "canvas": None}

    def _current_mode() -> str:
        return "export" if mode_var.get() == export_label else "diagnostic"

    def redraw(_event=None):
        for child in figure_frame.winfo_children():
            child.destroy()
        fig = figure_for(sr, mode=_current_mode(), lang=lang)
        figure_width = math.ceil(fig.get_figwidth() * fig.dpi)
        canvas_parent = figure_frame
        if figure_width > max_viewport_width:
            viewport = tk.Canvas(
                figure_frame, borderwidth=0, highlightthickness=0)
            scrollbar = ttk.Scrollbar(
                figure_frame, orient="horizontal", command=viewport.xview)
            scrollbar.pack(side="bottom", fill="x")
            viewport.pack(side="top", fill="both", expand=True)
            content = ttk.Frame(viewport)
            viewport.create_window((0, 0), window=content, anchor="nw")
            viewport.configure(xscrollcommand=scrollbar.set)
            content.bind(
                "<Configure>",
                lambda _e: viewport.configure(
                    scrollregion=viewport.bbox("all")))
            canvas_parent = content
        canvas = FigureCanvasTkAgg(fig, master=canvas_parent)
        canvas.draw()
        widget = canvas.get_tk_widget()
        widget.configure(width=figure_width)
        widget.pack(fill="both", expand=True)
        fig_ref["fig"] = fig
        fig_ref["canvas"] = canvas

    def save():
        path = filedialog.asksaveasfilename(
            parent=win, title=_dt(lang, "save_diagram"),
            defaultextension=".png",
            filetypes=[("PNG image", "*.png"), ("PDF", "*.pdf"),
                       ("SVG", "*.svg"), ("All files", "*.*")])
        if not path:
            return
        try:
            save_figure_transactionally(fig_ref["fig"], path)
        except Exception as e:                       # pragma: no cover
            messagebox.showerror(
                _dt(lang, "save_failed"),
                _diagram_save_error(path, e, lang), parent=win)
            return
        messagebox.showinfo(_dt(lang, "saved"),
                            _dt(lang, "saved_body", path=path), parent=win)

    ttk.Button(controls, text=_dt(lang, "save_image"),
               command=save).pack(side="right")
    mode_box.bind("<<ComboboxSelected>>", redraw)
    redraw()
    if hasattr(mode_box, "focus_set"):
        mode_box.focus_set()
