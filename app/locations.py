"""Recognise city and district names however they are written.

The names and their alternative spellings live in config/locations.json, which
you can edit (no code changes needed). Matching works in 3 steps:
    1. Clean the text: lower-case, unify Arabic letters, drop "حي" / "district"
       and the article "Al-" / "ال".   "حي النرجس" -> "نرجس",  "Al-Narjis" -> "narjis"
    2. Look it up in the alias list.   "نرجس" -> "Al-Narjis"
    3. If not found, allow a small typo ("Narjes" -> "Al-Narjis").
"""

import difflib
import json
import logging
from pathlib import Path

from app.normalize import normalize_text

logger = logging.getLogger(__name__)

# How similar a misspelling must be to count as a match (0-1). Higher = stricter.
FUZZY_CUTOFF = 0.8

_PREFIX_WORDS = ("حي", "district", "al", "el")


def place_key(value) -> str:
    """Reduce a place name to its core, for comparison. "" if empty."""
    words = normalize_text(value).split()
    if words and words[-1] == "district":
        words = words[:-1]
    while words and words[0] in _PREFIX_WORDS:
        words = words[1:]
    if words and words[0].startswith("ال") and len(words[0]) > 3:
        words[0] = words[0][2:]  # Arabic article: النرجس -> نرجس
    return " ".join(words)


class PlaceMatcher:
    """Maps any known spelling of a place to its canonical name."""

    def __init__(self, places: dict[str, list[str]]):
        self._lookup: dict[str, str] = {}
        for canonical, aliases in places.items():
            for name in [canonical, *aliases]:
                key = place_key(name)
                if key and self._lookup.setdefault(key, canonical) != canonical:
                    logger.warning("Alias %r is listed for two places; keeping the first", name)

    def match(self, raw) -> tuple[str, bool]:
        """Return (key, recognised).

        If recognised, key is the canonical name (e.g. "Al-Narjis"). If not,
        key is the cleaned text, so identical unknown names still match each other.
        """
        key = place_key(raw)
        if not key:
            return "", False
        if key in self._lookup:
            return self._lookup[key], True
        close = difflib.get_close_matches(key, self._lookup.keys(), n=1, cutoff=FUZZY_CUTOFF)
        if close:
            return self._lookup[close[0]], True
        return key, False


class Locations:
    def __init__(self, data: dict):
        self.cities = PlaceMatcher(data.get("cities", {}))
        self.districts = PlaceMatcher(data.get("districts", {}))

    @classmethod
    def load(cls, path: Path) -> "Locations":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return cls({k: v for k, v in data.items() if not k.startswith("_")})
