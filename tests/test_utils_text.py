"""Tests for fpl_cli.utils.text."""

import pytest

from fpl_cli.utils.text import ordinal_suffix, strip_diacritics


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
