"""Tests for fpl_cli.utils.text."""

import pytest

from fpl_cli.utils.text import (
    normalise_name,
    ordinal_suffix,
    ordinal_word,
    strip_diacritics,
)


@pytest.mark.parametrize(
    ("input_text", "expected"),
    [
        ("Gyökeres", "Gyokeres"),
        ("Raúl", "Raul"),
        ("Müller", "Muller"),
        ("Haaland", "Haaland"),
        ("", ""),
        ("Çalhanoğlu", "Calhanoglu"),
        ("Guéhi", "Guehi"),
        ("Sánchez", "Sanchez"),
        ("Cunhã", "Cunha"),
        ("Kadıoğlu", "Kadioglu"),
        ("Đalović", "Dalovic"),
        ("Łukasz", "Lukasz"),
        ("Ødegaard", "Odegaard"),
    ],
)
def test_strip_diacritics(input_text: str, expected: str) -> None:
    assert strip_diacritics(input_text) == expected


def test_strip_diacritics_preserves_case() -> None:
    assert strip_diacritics("GYÖKERES") == "GYOKERES"


@pytest.mark.parametrize(
    ("a", "b"),
    [
        # The two rewrites that cost real rows in a saved report (#343).
        ("B.Fernandes", "B. Fernandes"),
        ("O'Reilly", "O\u2019Reilly"),
        # The rest of the apostrophe family, either way round.
        ("N'Golo", "N\u02bcGolo"),
        ("N'Golo", "N\u2018Golo"),
        ("N'Golo", "N\u00b4Golo"),
        ("N'Golo", "N`Golo"),
        # What strip_diacritics and lower() already did, unchanged.
        ("Guéhi", "Guehi"),
        ("Gyökeres", "GYOKERES"),
        # Whitespace runs, non-breaking space included.
        ("Bruno Fernandes", "Bruno\u00a0Fernandes"),
        ("Bruno Fernandes", "  Bruno   Fernandes  "),
        # Both halves of the initial rule, in either direction.
        ("J.Ramsey", "J. Ramsey"),
    ],
)
def test_normalise_name_folds_equivalent_spellings(a: str, b: str) -> None:
    assert normalise_name(a) == normalise_name(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        # Different players must not collide just because folding is generous.
        ("Salah", "Sala"),
        ("B.Fernandes", "Fernandes"),
        ("O'Reilly", "Reilly"),
    ],
)
def test_normalise_name_keeps_distinct_names_distinct(a: str, b: str) -> None:
    assert normalise_name(a) != normalise_name(b)


def test_normalise_name_leaves_multi_letter_abbreviations_alone() -> None:
    """Only a single letter is an initial - "Jr." keeps the space after it."""
    assert normalise_name("Vinicius Jr. Silva") == "vinicius jr. silva"


def test_normalise_name_empty() -> None:
    assert normalise_name("") == ""


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (1, "st"), (2, "nd"), (3, "rd"), (4, "th"),
        # The exception the three private copies each spelt differently.
        (11, "th"), (12, "th"), (13, "th"),
        (21, "st"), (22, "nd"), (23, "rd"),
        (111, "th"), (112, "th"), (113, "th"),
        (101, "st"), (45170, "th"),
    ],
)
def test_ordinal_suffix(n: int, expected: str) -> None:
    assert ordinal_suffix(n) == expected


@pytest.mark.parametrize(
    ("n", "expected"),
    [(1, "first"), (2, "second"), (3, "third"), (10, "tenth"), (11, "11th"), (21, "21st"), (22, "22nd")],
)
def test_ordinal_word(n: int, expected: str) -> None:
    assert ordinal_word(n) == expected
