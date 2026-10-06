"""Small helpers to clean up text and numbers typed by people (in the sheet or in chats).

Used so that "Al-Narjis", "al narjis" and "حي النرجس" can all be recognised as the
same thing, and "950,000" or "٩٥٠٠٠٠" are read as the number 950000.
"""

import math
import re

# Arabic short vowels (tashkeel) and the tatweel stretching character "ـ".
_ARABIC_MARKS = re.compile(r"[ً-ْٰـ]")

# Letters that people write in several ways: أ إ آ -> ا, ة -> ه, ى -> ي
_ARABIC_LETTERS = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ة": "ه", "ى": "ي"})

# Arabic-Indic and Persian digits -> 0-9, Arabic decimal/thousands separators -> . and ,
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫٬", "01234567890123456789.,")

_SEPARATORS = re.compile(r"[-_.,،/\\()]+")
_SPACES = re.compile(r"\s+")


def normalize_text(value) -> str:
    """Lower-case, trim, and unify spelling variants. Returns "" for empty values.

    >>> normalize_text("  Al-Narjis ")
    'al narjis'
    >>> normalize_text("حيّ النرجس")
    'حي النرجس'
    """
    if value is None:
        return ""
    text = str(value).strip().lower()
    text = _ARABIC_MARKS.sub("", text)
    text = text.translate(_ARABIC_LETTERS).translate(_DIGITS)
    text = _SEPARATORS.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


def clean_str(value) -> str | None:
    """Trim a cell value to a string; None if empty. 7200000001.0 -> "7200000001"."""
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    return text or None


def parse_number(value) -> int | float | None:
    """Read a number from a sheet cell. Returns None if it isn't a plain number.

    Accepts: 950000, 950000.0, "950000", "950,000", "٩٥٠٬٠٠٠", " 950 000 ".
    Rejects: "call us", "950k", "950000 SAR", "", negative numbers.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = value
    else:
        text = str(value).strip().translate(_DIGITS)
        text = re.sub(r"[,\s ]", "", text)  # thousands separators and spaces
        if not re.fullmatch(r"\d+(\.\d+)?", text):
            return None
        number = float(text)
    if not math.isfinite(number) or number < 0:
        return None
    return int(number) if float(number).is_integer() else float(number)


def id_key(property_id) -> str:
    """Comparison key for property IDs, so "P-1023", "p1023" and "P 1023" all match.

    Arabic letters are kept: "بيع سكني 12" and "إيجار سكني 12" are different properties.
    """
    return re.sub(r"[^0-9a-z\u0621-\u064a]", "", normalize_text(property_id))


# "290 ألف", "11الف", "1.5 مليون", "2م" (million in prices): the words that multiply a number.
_THOUSAND_WORDS = ("الف", "آلاف", "الاف", "ألف", "k")
_MILLION_WORDS = ("مليون", "ملايين", "million")


def parse_amount(value, million_suffix: bool = False) -> int | float | None:
    """The first number in free text, with Arabic "thousand"/"million" words applied.

    "290 ألف للقطعة الواحدة" -> 290000, "3الف" -> 3000, "315م" -> 315, "1.5 مليون" -> 1500000.
    With million_suffix=True (prices), a trailing "م" means million: "1م" -> 1000000.
    Returns None if there is no number.
    """
    number = parse_number(value)
    if number is not None:
        return number
    if value is None:
        return None
    text = str(value).translate(_DIGITS).replace(",", "")
    match = re.search(r"\d+(\.\d+)?", text)
    if not match:
        return None
    amount = float(match.group(0))
    after = text[match.end():].strip().lower()
    if any(after.startswith(w) for w in _MILLION_WORDS) or (million_suffix and re.match(r"م(\s|$)", after)):
        amount *= 1_000_000
    elif any(after.startswith(w) for w in _THOUSAND_WORDS):
        amount *= 1000
    return int(amount) if amount.is_integer() else amount


# A phone number: 9 or more digits (Western, Arabic-Indic or Persian), optionally starting
# with "+" and split by spaces or dashes. Prices like "3,450,000" (commas) and areas stay.
_PHONE = re.compile(r"\+?[0-9٠-٩۰-۹](?:[\s-]?[0-9٠-٩۰-۹]){8,}")


def remove_phone_numbers(text: str | None) -> str | None:
    """Remove anything that looks like a phone number (the bot never shares numbers)."""
    if not text:
        return text
    cleaned = _PHONE.sub("", text)
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip() if cleaned != text else text
