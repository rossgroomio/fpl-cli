"""Text utilities for cross-source name comparison."""

import unicodedata

_LETTER_VARIANTS: dict[int, str] = {
    0x0131: "i",  # ı → i  (Turkish dotless i)
    0x0130: "I",  # İ → I  (Turkish dotted capital I)
    0x0111: "d",  # đ → d  (Croatian/Vietnamese d with stroke)
    0x0110: "D",  # Đ → D
    0x0142: "l",  # ł → l  (Polish l with stroke)
    0x0141: "L",  # Ł → L
    0x00F8: "o",  # ø → o  (Scandinavian o with stroke)
    0x00D8: "O",  # Ø → O
}


def strip_diacritics(text: str) -> str:
    """Remove diacritical marks so e.g. 'Gyökeres' becomes 'Gyokeres'.

    Handles both NFD-decomposable accents and non-decomposable letter variants
    (e.g. Turkish ı, Polish ł) via a translation table.
    """
    text = text.translate(_LETTER_VARIANTS)
    return "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )
