"""Tests for fpl_cli.utils.text."""

import pytest

from fpl_cli.utils.text import strip_diacritics


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
