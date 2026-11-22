"""Exact rational arithmetic for subscription shares.

All entitlements, split ratios and balances are :class:`fractions.Fraction`
values - never floats - so a 2:1 split stays exactly ``2/3`` / ``1/3`` through
merges, multi-period accumulation and the scheduler's balance comparisons.

Text form is the canonical ``"n/d"`` (or ``"n"`` when the denominator is 1);
inputs also accept decimal-looking strings such as ``"0.5"`` and bare integers.
"""
from __future__ import annotations

from fractions import Fraction
from typing import Any

#: Public alias for the exact rational type used everywhere in the subsystem.
Q = Fraction


def frac_to_text(value: Fraction) -> str:
    """Canonical text form stored in SQLite (always lowest terms)."""
    if not isinstance(value, Fraction):
        raise TypeError("expected a Fraction")
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def frac_from_text(text: Any) -> Fraction:
    """Parse ``"n/d"``, ``"n"`` or a finite decimal string into a :class:`Fraction`.

    Raises :class:`ValueError` on anything that is not a non-empty exact
    rational literal.  Scientific notation is deliberately rejected: shares
    must be written as exact fractions or terminating decimals.
    """
    if isinstance(text, Fraction):
        return text
    if isinstance(text, int) and not isinstance(text, bool):
        return Fraction(text)
    if not isinstance(text, str):
        raise ValueError(f"not a rational literal: {text!r}")
    raw = text.strip()
    if not raw:
        raise ValueError("empty rational literal")
    if "/" in raw:
        numerator, slash, denominator = raw.partition("/")
        numerator = numerator.strip()
        denominator = denominator.strip()
        if not numerator or not denominator or "/" in denominator:
            raise ValueError(f"bad fraction literal: {text!r}")
        try:
            result = Fraction(int(numerator), int(denominator))
        except (ValueError, ZeroDivisionError) as exc:
            raise ValueError(f"bad fraction literal: {text!r}") from exc
        return result
    if "." in raw:
        whole, _, fractional = raw.partition(".")
        whole = whole.strip() or "0"
        fractional = fractional.strip()
        if not fractional or not whole.lstrip("-").isdigit() or not fractional.isdigit():
            raise ValueError(f"bad decimal literal: {text!r}")
        scale = 10 ** len(fractional)
        return Fraction(int(whole) * scale + int(fractional), scale)
    if not raw.lstrip("-").isdigit():
        raise ValueError(f"bad rational literal: {text!r}")
    return Fraction(int(raw))
