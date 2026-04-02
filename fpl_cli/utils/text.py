"""Text utilities for cross-source name comparison."""

import unicodedata


def strip_diacritics(text: str) -> str:
    """Remove diacritical marks so e.g. 'Gyökeres' becomes 'Gyokeres'."""
    return "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )
