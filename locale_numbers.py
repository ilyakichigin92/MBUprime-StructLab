"""Locale-tolerant numeric parsing shared by GUI and command-line inputs."""

from __future__ import annotations


def parse_locale_number(raw: str, *, integer: bool = False) -> int | float:
    """Parse one decimal separator without relying on the process locale."""

    token = raw.strip()
    separator_count = token.count(".") + token.count(",")
    if integer:
        if separator_count:
            raise ValueError("integer fields do not accept decimal separators")
        return int(token, 10)
    if separator_count > 1:
        raise ValueError("use exactly one dot or comma as the decimal separator")
    return float(token.replace(",", "."))
