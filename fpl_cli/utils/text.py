"""Text utilities: cross-source name comparison, and number wording."""

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


_ORDINAL_SUFFIXES = {1: "st", 2: "nd", 3: "rd"}


def ordinal_suffix(n: int) -> str:
    """The "st"/"nd"/"rd"/"th" for a number, minus the number itself.

    Only the suffix, because that is the only part every caller agrees on:
    the status line prints a grouped overall rank ("45,170th"), the recap
    report a bare league position ("3rd"), and the recap prompt spells the
    small numbers out in words. Three copies of the 11-12-13 exception were
    already in the tree, which is one rule in three places waiting to
    disagree.
    """
    return "th" if 11 <= n % 100 <= 13 else _ORDINAL_SUFFIXES.get(n % 10, "th")


# Spelt out to the tenth, which covers every count the recap prose realistically
# reaches -- fines in a season, seasons of FPL played; past that the numeral is
# clearer than the word anyway.
_ORDINAL_WORDS = (
    "first", "second", "third", "fourth", "fifth",
    "sixth", "seventh", "eighth", "ninth", "tenth",
)


def ordinal_word(n: int) -> str:
    """"first", "second", ... "tenth", then "11th", "21st", "22nd".

    How the recap prose spells an ordinal: the fines placement ("their third
    of the season") and the prior-seasons line ("their third season of FPL")
    land in the same prompt and the same report, so they share one spelling.
    """
    if 1 <= n <= len(_ORDINAL_WORDS):
        return _ORDINAL_WORDS[n - 1]
    return f"{n}{ordinal_suffix(n)}"
