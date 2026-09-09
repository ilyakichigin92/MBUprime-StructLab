"""Tk- and matplotlib-free ASCII rendering helpers for scientific reports."""

from __future__ import annotations

def _parse_rows(ascii_structure: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for line in ascii_structure.splitlines():
        if not line.strip("\n"):
            continue
        if "\t" in line:
            tag, content = line.split("\t", 1)
        else:
            tag, content = "", line
        rows.append((tag, content))
    return rows


def _reconstruct_dimer(
    rows: list[tuple[str, str]],
) -> tuple[str, str, list[tuple[int, int]]]:
    sequence_rows = [content for tag, content in rows if tag == "SEQ"]
    structure_rows = [content for tag, content in rows if tag == "STR"]
    if not sequence_rows or not structure_rows:
        return "", "", []
    width = max(len(content) for content in sequence_rows + structure_rows)
    sequence_rows = [content.ljust(width) for content in sequence_rows]
    structure_rows = [content.ljust(width) for content in structure_rows]
    inner_top, inner_bottom = sequence_rows[-1], structure_rows[0]

    def base_at(tracks: list[str], column: int) -> str | None:
        return next(
            (track[column] for track in tracks if track[column] not in " -"),
            None,
        )

    top: list[str] = []
    bottom: list[str] = []
    top_indices: dict[int, int] = {}
    bottom_indices: dict[int, int] = {}
    for column in range(width):
        base = base_at(sequence_rows, column)
        if base:
            top_indices[column] = len(top)
            top.append(base)
        base = base_at(structure_rows, column)
        if base:
            bottom_indices[column] = len(bottom)
            bottom.append(base)
    pairs = [
        (top_indices[column], bottom_indices[column])
        for column in range(width)
        if inner_top[column] not in " -"
        and inner_bottom[column] not in " -"
        and column in top_indices
        and column in bottom_indices
    ]
    return "".join(top), "".join(bottom), pairs


def _duplex_text(
    top: str, bottom_reversed: str, pairs: list[tuple[int, int]], offset: int,
) -> str:
    ordered_pairs = sorted(pairs)
    monotonic = all(
        left > previous_left and right > previous_right
        for (previous_left, previous_right), (left, right)
        in zip(ordered_pairs, ordered_pairs[1:])
    )
    in_bounds = all(
        0 <= left < len(top) and 0 <= right < len(bottom_reversed)
        for left, right in ordered_pairs
    )
    if ordered_pairs and monotonic and in_bounds:
        first_left, first_right = ordered_pairs[0]
        prefix_width = max(first_left, first_right)
        top_line = [" "] * (prefix_width - first_left) + list(top[:first_left])
        bottom_line = (
            [" "] * (prefix_width - first_right)
            + list(bottom_reversed[:first_right])
        )
        bond_line = [" "] * prefix_width
        top_line.append(top[first_left])
        bottom_line.append(bottom_reversed[first_right])
        bond_line.append("|")

        previous_left, previous_right = ordered_pairs[0]
        for left, right in ordered_pairs[1:]:
            top_segment = top[previous_left + 1:left]
            bottom_segment = bottom_reversed[previous_right + 1:right]
            segment_width = max(len(top_segment), len(bottom_segment))
            top_line.extend(top_segment + "-" * (segment_width - len(top_segment)))
            bottom_line.extend(
                bottom_segment + "-" * (segment_width - len(bottom_segment)))
            bond_line.extend(" " * segment_width)
            top_line.append(top[left])
            bottom_line.append(bottom_reversed[right])
            bond_line.append("|")
            previous_left, previous_right = left, right

        top_suffix = top[previous_left + 1:]
        bottom_suffix = bottom_reversed[previous_right + 1:]
        suffix_width = max(len(top_suffix), len(bottom_suffix))
        top_line.extend(top_suffix + " " * (suffix_width - len(top_suffix)))
        bottom_line.extend(
            bottom_suffix + " " * (suffix_width - len(bottom_suffix)))
        bond_line.extend(" " * suffix_width)
        return (
            f"5'-{''.join(top_line)}-3'\n"
            f"   {''.join(bond_line)}\n"
            f"3'-{''.join(bottom_line)}-5'"
        )

    left_shift = max(0, -offset)
    width = max(
        left_shift + len(top),
        offset + left_shift + len(bottom_reversed),
    )
    top_line = [" "] * width
    for index, base in enumerate(top):
        top_line[left_shift + index] = base
    bottom_line = [" "] * width
    for index, base in enumerate(bottom_reversed):
        bottom_line[offset + left_shift + index] = base
    bond_line = [" "] * width
    text = (
        f"5'-{''.join(top_line)}-3'\n"
        f"   {''.join(bond_line)}\n"
        f"3'-{''.join(bottom_line)}-5'"
    )
    if ordered_pairs:
        pair_map = ", ".join(f"{left}:{right}" for left, right in ordered_pairs)
        text += f"\n   pair map (0-based top:3'-row): {pair_map}"
    return text


def display_ascii(structure: object) -> str:
    """Render a ranked structure as deterministic plain ASCII."""

    geometry = getattr(structure, "canonical_geometry", None)
    if geometry is not None:
        if geometry.kind == "hairpin":
            pattern = ["-"] * len(geometry.sequence_a)
            for left, right in geometry.pairs:
                pattern[left], pattern[right] = "/", "\\"
            return f"{''.join(pattern)}\n{geometry.sequence_a}"
        bottom = (geometry.sequence_b or "")[::-1]
        bottom_length = len(bottom)
        display_pairs = [
            (left, bottom_length - 1 - right)
            for left, right in geometry.pairs
        ]
        offset = (
            display_pairs[0][0] - display_pairs[0][1]
            if display_pairs else 0
        )
        return _duplex_text(geometry.sequence_a, bottom, display_pairs, offset)

    if hasattr(structure, "mode"):
        if structure.mode == "hairpin":
            return f"{structure.hp_pattern}\n{structure.hp_seq}"
        bottom = structure.du_b[::-1]
        bottom_length = len(bottom)
        pairs = [
            (left, bottom_length - 1 - right)
            for left, right in structure.du_pairs
        ]
        return _duplex_text(structure.du_a, bottom, pairs, structure.du_offset)

    rows = _parse_rows(getattr(structure, "structure", ""))
    if getattr(structure, "kind", "") == "Hairpin" and len(rows) >= 2:
        return f"{rows[0][1]}\n{rows[1][1]}"
    top, bottom, pairs = _reconstruct_dimer(rows)
    offset = pairs[0][0] - pairs[0][1] if pairs else 0
    return _duplex_text(top, bottom, pairs, offset)


def is_drawable(structure: object) -> bool:
    """Return whether a found structure has reportable geometry."""

    if structure is None or not getattr(structure, "found", False):
        return False
    geometry = getattr(structure, "canonical_geometry", None)
    if geometry is not None and bool(getattr(geometry, "pairs", ())):
        return True
    return bool(getattr(structure, "structure", ""))
